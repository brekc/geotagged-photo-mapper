"""oriented_imagery.py

Builds an Esri Oriented Imagery table (see
https://doc.esri.com/en/arcgis-pro/latest/help/data/imagery/oriented-imagery-table.html)
from photos already mapped by geotagged_photo_mapper.py.

Only generic EXIF is understood in this version. Camera pose fields
(CameraPitch/CameraRoll/Omega/Phi/Kappa/Matrix/principal-point/distortion)
are always left blank: populating them requires a documented, fixture-tested
adapter for a specific camera/gimbal metadata convention, and guessing would
silently corrupt anyone's orientation data. When vendor-specific pose tags
(e.g. DJI's drone-dji XMP fields) are detected, a row warning says so instead
of converting them.

Two export modes:

  * Reference mode (Mode A) points ImagePath at images that already exist
    somewhere the *end user's* machine can read (a local path, a UNC share,
    or an http(s) URL) and writes only oriented_imagery.csv. The server
    never opens those images -- it cannot verify the path actually resolves.

  * Portable mode (Mode B) re-receives the currently-visible photos as
    freshly reposted browser File uploads, re-extracts their metadata
    server-side, converts each to a privacy-stripped, orientation-normalized
    JPEG derivative, and packages oriented_imagery.csv + manifest.json +
    README.txt + images/*.jpg into a ZIP.
"""

import csv
import hashlib
import io
import json
import math
import os
import re
import time
import zipfile
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from PIL import Image, ImageOps

ESRI_DOC_URL = 'https://doc.esri.com/en/arcgis-pro/latest/help/data/imagery/oriented-imagery-table.html'

# ---------------------------------------------------------------------------
# Table schema
# ---------------------------------------------------------------------------

# Esri's documented Oriented Imagery table columns, in a stable order.
CORE_FIELDS = [
    'X', 'Y', 'ImagePath', 'SRS', 'Name', 'Z', 'AcquisitionDate',
    'CameraHeading', 'CameraPitch', 'CameraRoll',
    'HorizontalFieldOfView', 'VerticalFieldOfView', 'ImageRotation',
    'OrientedImageryType', 'Omega', 'Phi', 'Kappa', 'Matrix',
    'FocalLength', 'PrincipalX', 'PrincipalY', 'Radial', 'Tangential',
    'A0', 'A1', 'A2', 'B0', 'B1', 'B2', 'SequenceOrder',
]

# Audit/provenance columns this app adds after the core Esri fields.
AUDIT_FIELDS = [
    'Make', 'Model', 'FocalLength35mmEq', 'MetadataSource',
    'OrientationStatus', 'RowWarnings',
]

ORIENTED_IMAGERY_FIELDS = CORE_FIELDS + AUDIT_FIELDS

# Fields never populated in this generic-EXIF-only version. Always blank.
_ALWAYS_BLANK_FIELDS = (
    'CameraPitch', 'CameraRoll', 'Omega', 'Phi', 'Kappa', 'Matrix',
    'PrincipalX', 'PrincipalY', 'Radial', 'Tangential',
    'A0', 'A1', 'A2', 'B0', 'B1', 'B2',
)

# Free-text fields that can contain attacker/camera-controlled strings and
# so need CSV formula-injection escaping. Numeric fields are computed by
# this app from validated floats and are written as plain numbers instead,
# since prefixing a legitimate negative coordinate with a quote would
# corrupt it for GIS ingestion.
_TEXT_FIELDS = {
    'ImagePath', 'SRS', 'Name', 'AcquisitionDate', 'OrientedImageryType',
    'Matrix', 'Make', 'Model', 'MetadataSource', 'OrientationStatus',
    'RowWarnings',
}

ORIENTED_IMAGERY_TYPES = ('Horizontal', 'Oblique', 'Nadir', '360', 'Inspection')

VENDOR_POSE_TAGS = (
    'XMP:GimbalYawDegree', 'XMP:GimbalPitchDegree', 'XMP:GimbalRollDegree',
    'XMP:FlightYawDegree', 'XMP:FlightPitchDegree', 'XMP:FlightRollDegree',
)

# ---------------------------------------------------------------------------
# CSV safety
# ---------------------------------------------------------------------------

_CSV_DANGEROUS_PREFIXES = ('=', '+', '-', '@', '\t', '\r')


def csv_safe_text(value) -> str:
    """Escape a free-text CSV cell against formula injection (OWASP-style:
    prefix a leading =, +, -, @, tab, or CR with an apostrophe). Never
    applied to numeric fields -- see _TEXT_FIELDS -- since quoting a
    legitimate negative coordinate would corrupt it for GIS ingestion."""
    if value is None:
        return ''
    text = str(value)
    if text.startswith(_CSV_DANGEROUS_PREFIXES):
        return "'" + text
    return text


def _format_cell(field_name: str, value) -> str:
    if value is None or value == '':
        return ''
    if field_name in _TEXT_FIELDS:
        return csv_safe_text(value)
    if isinstance(value, float):
        text = f'{value:.8f}'.rstrip('0').rstrip('.')
        return text or '0'
    return str(value)


def write_oriented_imagery_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\r\n')
    writer.writerow(ORIENTED_IMAGERY_FIELDS)
    for row in rows:
        writer.writerow([_format_cell(f, row.get(f)) for f in ORIENTED_IMAGERY_FIELDS])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Base-location (Mode A) validation
# ---------------------------------------------------------------------------

_ALLOWED_URL_SCHEMES = {'http', 'https'}
_MAX_BASE_LOCATION_LENGTH = 500
_CONTROL_CHAR_RE = re.compile(r'[\x00-\x1f\x7f]')


class InvalidBaseLocation(ValueError):
    pass


def validate_base_location(raw: str) -> tuple[str, str]:
    """Validate a Mode A base path/URL. Returns (kind, normalized) where
    kind is 'local', 'unc', or 'url'. Raises InvalidBaseLocation otherwise.
    This never touches the filesystem or network -- it cannot confirm the
    location actually exists, only that its *shape* is safe to write into a
    CSV that ArcGIS Pro will later resolve on someone else's machine.
    """
    text = (raw or '').strip()
    if not text:
        raise InvalidBaseLocation('A base path or URL is required.')
    if len(text) > _MAX_BASE_LOCATION_LENGTH:
        raise InvalidBaseLocation('Base path/URL is too long.')
    if _CONTROL_CHAR_RE.search(text):
        raise InvalidBaseLocation('Base path/URL contains control characters.')
    if '..' in text.replace('\\', '/').split('/'):
        raise InvalidBaseLocation('Base path/URL may not contain ".." path segments.')

    if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://', text):
        parts = urlsplit(text)
        if parts.scheme.lower() not in _ALLOWED_URL_SCHEMES:
            raise InvalidBaseLocation(f'Unsupported URL scheme: {parts.scheme}')
        if '@' in parts.netloc:
            raise InvalidBaseLocation('URLs with embedded credentials are not allowed.')
        if not parts.hostname:
            raise InvalidBaseLocation('URL is missing a host.')
        normalized = text if text.endswith('/') else text + '/'
        return 'url', normalized

    if text.startswith('\\\\'):
        return 'unc', text if text.endswith('\\') else text + '\\'

    if re.match(r'^[A-Za-z]:[\\/]', text) or text.startswith('/'):
        sep = '\\' if '\\' in text else '/'
        return 'local', text if text.endswith(('\\', '/')) else text + sep

    raise InvalidBaseLocation(
        'Enter an absolute local path (C:\\...), a UNC path (\\\\server\\share\\...), '
        'or an http(s):// URL.'
    )


MODE_A_ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.tif', '.tiff'}
MODE_A_WARN_EXTENSIONS = {'.png', '.heic', '.heif'}

# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------


@dataclass
class RowResult:
    row: dict | None
    status: str  # complete | partial | minimum-only | excluded | error
    warnings: list[str] = field(default_factory=list)


def _orientation_rotation_degrees(orientation) -> tuple[int | None, bool]:
    """EXIF Orientation -> (rotation degrees to correct for display, is_mirrored).
    Esri's ImageRotation field represents a plain rotation; it cannot express
    a mirror, so mirrored orientations are reported via the warning list
    instead of guessing a value."""
    mapping = {1: (0, False), 3: (180, False), 6: (270, False), 8: (90, False),
               2: (0, True), 4: (180, True), 5: (270, True), 7: (90, True)}
    return mapping.get(orientation, (None, False))


def _estimate_fov(focal_35mm, width, height, orientation) -> tuple[float | None, float | None]:
    """Approximate horizontal/vertical FOV from a 35mm-equivalent focal
    length, assuming the standard 36x24mm reference frame. Explicitly
    labeled approximate -- this is not a calibrated camera model."""
    if not focal_35mm or focal_35mm <= 0:
        return None, None
    h_fov = 2 * math.degrees(math.atan(36.0 / (2 * focal_35mm)))
    v_fov = 2 * math.degrees(math.atan(24.0 / (2 * focal_35mm)))
    is_portrait = False
    if width and height:
        is_portrait = height > width
    if orientation in (5, 6, 7, 8):
        is_portrait = not is_portrait
    if is_portrait:
        h_fov, v_fov = v_fov, h_fov
    return round(h_fov, 1), round(v_fov, 1)


def build_row(
    meta: dict,
    display_name: str,
    image_path: str,
    srs_label: str,
    oriented_imagery_type: str,
    sequence_order: int,
    pixels_normalized: bool,
) -> RowResult:
    """Build one Oriented Imagery row from a normalized photo-metadata dict
    (the same shape geotagged_photo_mapper.extract_gps() produces).
    `pixels_normalized` is True in portable mode, where the derivative JPEG
    has already had EXIF orientation baked into its pixels.
    """
    try:
        lat = meta.get('latitude')
        lon = meta.get('longitude')
        if lat is None or lon is None:
            return RowResult(row=None, status='excluded', warnings=['no_gps_data'])

        warnings: list[str] = []
        row: dict = {f: '' for f in ORIENTED_IMAGERY_FIELDS}
        row['X'] = lon
        row['Y'] = lat
        row['ImagePath'] = image_path
        row['SRS'] = srs_label
        row['Name'] = display_name
        row['OrientedImageryType'] = oriented_imagery_type
        row['SequenceOrder'] = sequence_order
        row['Make'] = meta.get('camera_make') or ''
        row['Model'] = meta.get('camera_model') or ''
        row['MetadataSource'] = 'generic_exif'

        altitude_m = meta.get('altitude_m')
        if altitude_m is not None:
            row['Z'] = altitude_m
            warnings.append('unknown_altitude_datum')

        dt = meta.get('datetime')
        has_date = bool(dt)
        if dt:
            subsec = meta.get('subsec_time_original')
            offset = meta.get('offset_time_original')
            acquisition = str(dt)
            if subsec:
                acquisition += f'.{subsec}'
            if offset:
                acquisition += str(offset)
            else:
                warnings.append('missing_timezone')
            row['AcquisitionDate'] = acquisition

        has_true_heading = False
        if meta.get('heading_deg') is not None:
            if meta.get('heading_is_true'):
                row['CameraHeading'] = meta['heading_deg']
                has_true_heading = True
            else:
                warnings.append('magnetic_heading_unsupported')

        focal = meta.get('focal_length_mm')
        if focal is not None:
            row['FocalLength'] = focal
        focal35 = meta.get('focal_length_35mm_eq')
        has_fov = False
        if focal35 is not None:
            row['FocalLength35mmEq'] = focal35
            h_fov, v_fov = _estimate_fov(
                focal35, meta.get('pixel_width'), meta.get('pixel_height'), meta.get('orientation'),
            )
            if h_fov is not None:
                row['HorizontalFieldOfView'] = h_fov
                row['VerticalFieldOfView'] = v_fov
                has_fov = True
                warnings.append('approximate_fov_35mm_equivalent')

        if pixels_normalized:
            row['ImageRotation'] = 0
        else:
            rotation, mirrored = _orientation_rotation_degrees(meta.get('orientation'))
            if rotation is not None:
                row['ImageRotation'] = rotation
            if mirrored:
                warnings.append('mirrored_orientation_unsupported')

        if meta.get('vendor_pose_detected'):
            warnings.append('vendor_pose_metadata_unsupported')

        score = sum([has_date, has_true_heading, has_fov])
        status = 'complete' if score == 3 else ('minimum-only' if score == 0 else 'partial')

        row['OrientationStatus'] = status
        row['RowWarnings'] = ';'.join(warnings)
        return RowResult(row=row, status=status, warnings=warnings)
    except Exception as e:  # noqa: BLE001 - one bad row must not abort the batch
        return RowResult(row=None, status='error', warnings=[f'row_build_error: {e}'])


def build_preflight(rows_meta: list[dict]) -> dict:
    """Per-batch counts for the Build Oriented Imagery preview panel."""
    total = len(rows_meta)
    valid_gps = sum(1 for r in rows_meta if r.get('latitude') is not None)
    has_date = sum(1 for r in rows_meta if r.get('datetime'))
    true_heading = sum(1 for r in rows_meta if r.get('heading_is_true'))
    focal_length = sum(1 for r in rows_meta if r.get('focal_length_mm') is not None)
    approx_fov = sum(1 for r in rows_meta if r.get('focal_length_35mm_eq') is not None)
    advanced_pose = 0  # never populated by the generic-EXIF adapter
    requires_conversion = sum(
        1 for r in rows_meta if str(r.get('filename', '')).lower().endswith(('.heic', '.heif', '.png'))
    )
    excluded = total - valid_gps
    warnings = sum(1 for r in rows_meta if r.get('vendor_pose_detected'))
    return {
        'total_files': total,
        'valid_gps': valid_gps,
        'acquisition_date_available': has_date,
        'true_heading_available': true_heading,
        'focal_length_available': focal_length,
        'approximate_fov_available': approx_fov,
        'advanced_pose_available': advanced_pose,
        'files_requiring_conversion': requires_conversion,
        'excluded_files': excluded,
        'warnings': warnings,
    }


# ---------------------------------------------------------------------------
# Filenames
# ---------------------------------------------------------------------------


def _safe_stem(name: str) -> str:
    stem = os.path.splitext(os.path.basename(name or ''))[0]
    stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem).strip('._')
    return stem[:80] or 'photo'


def derivative_filename(index: int, original_display_name: str) -> str:
    """Deterministic and collision-safe: the zero-padded sequence index
    guarantees uniqueness even when two originals sanitize to the same
    stem (e.g. IMG_0001.HEIC and IMG_0001.JPG)."""
    return f'{index:04d}_{_safe_stem(original_display_name)}.jpg'


# ---------------------------------------------------------------------------
# Mode A: reference existing images
# ---------------------------------------------------------------------------


def build_reference_export(
    rows_meta: list[dict],
    base_location: str,
    srs_label: str,
    oriented_imagery_type: str,
) -> dict:
    """Build oriented_imagery.csv for Mode A. Returns a dict with the CSV
    text, a short preview of resolved paths, per-file warnings, and counts.
    """
    kind, normalized_base = validate_base_location(base_location)
    sep = '\\' if kind in ('local', 'unc') else '/'

    rows = []
    file_warnings = []
    excluded = 0
    for i, meta in enumerate(rows_meta, start=1):
        display_name = meta.get('filename', f'photo_{i}')
        ext = os.path.splitext(display_name)[1].lower()

        if ext in MODE_A_WARN_EXTENSIONS:
            file_warnings.append({
                'filename': display_name,
                'warning': f'{ext} is not an Esri-documented source-image format for reference mode; excluded.',
            })
            excluded += 1
            continue
        if ext not in MODE_A_ALLOWED_EXTENSIONS:
            file_warnings.append({'filename': display_name, 'warning': f'Unsupported source format: {ext or "unknown"}'})
            excluded += 1
            continue

        clean_name = display_name.replace('\\', '').replace('/', '')
        if kind == 'url':
            image_path = normalized_base + clean_name
        else:
            image_path = normalized_base.rstrip(sep) + sep + clean_name

        result = build_row(meta, display_name, image_path, srs_label, oriented_imagery_type, i, pixels_normalized=False)
        if result.row is None:
            file_warnings.append({'filename': display_name, 'warning': '; '.join(result.warnings) or 'excluded'})
            excluded += 1
            continue
        rows.append(result.row)
        if result.warnings:
            file_warnings.append({'filename': display_name, 'warning': '; '.join(result.warnings)})

    csv_text = write_oriented_imagery_csv(rows)
    preview_paths = [r['ImagePath'] for r in rows[:5]]

    return {
        'csv': csv_text,
        'kind': kind,
        'preview_paths': preview_paths,
        'row_count': len(rows),
        'excluded_count': excluded,
        'warnings': file_warnings,
        'note': (
            'The server validated the shape of this base location only. It cannot '
            'confirm these paths actually resolve -- that depends on the machine '
            'that opens the table in ArcGIS Pro.'
        ),
    }


# ---------------------------------------------------------------------------
# Mode B: portable package
# ---------------------------------------------------------------------------

MAX_PORTABLE_ZIP_BYTES = 300 * 1024 * 1024
_PORTABLE_JPEG_QUALITY = 90
_MAX_ICC_PROFILE_BYTES = 10_000


class PortablePackageTooLarge(ValueError):
    pass


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_derivative_jpeg(source_path: str) -> bytes:
    """Orientation-normalized, privacy-stripped JPEG derivative. Only a
    small, bounded ICC profile is carried over; no EXIF/XMP/GPS/thumbnail
    data is written to the output at all since we never pass any of it to
    save()."""
    with Image.open(source_path) as img:
        img.load()
        img = ImageOps.exif_transpose(img)
        img = img.convert('RGB')
        icc = img.info.get('icc_profile')
        save_kwargs = {'format': 'JPEG', 'quality': _PORTABLE_JPEG_QUALITY}
        if icc and len(icc) <= _MAX_ICC_PROFILE_BYTES:
            save_kwargs['icc_profile'] = icc
        buf = io.BytesIO()
        img.save(buf, **save_kwargs)
        return buf.getvalue()


@dataclass
class PortableItem:
    display_name: str
    source_path: str


def build_portable_package(
    items: list[PortableItem],
    rows_meta: list[dict],
    srs_label: str,
    oriented_imagery_type: str,
    export_name: str,
) -> bytes:
    """Build the full portable ZIP (csv + manifest + README + images/*.jpg)
    in memory. Raises PortablePackageTooLarge if the bounded size is
    exceeded. Caller is responsible for deleting `items[*].source_path`
    (request-scoped temp files) in a finally block.
    """
    meta_by_name = {m.get('filename'): m for m in rows_meta}

    rows = []
    manifest_entries = []
    file_warnings = []
    total_bytes = 0
    images: dict[str, bytes] = {}

    for i, item in enumerate(items, start=1):
        meta = meta_by_name.get(item.display_name)
        if meta is None or meta.get('latitude') is None:
            file_warnings.append({'filename': item.display_name, 'warning': 'no_gps_data'})
            continue

        try:
            derivative_bytes = _build_derivative_jpeg(item.source_path)
        except Exception as e:  # noqa: BLE001
            file_warnings.append({'filename': item.display_name, 'warning': f'conversion_failed: {e}'})
            continue

        total_bytes += len(derivative_bytes)
        if total_bytes > MAX_PORTABLE_ZIP_BYTES:
            raise PortablePackageTooLarge(
                f'Portable package exceeds the {MAX_PORTABLE_ZIP_BYTES // (1024 * 1024)} MB limit.'
            )

        deriv_name = derivative_filename(i, item.display_name)
        image_path = f'images/{deriv_name}'
        images[deriv_name] = derivative_bytes

        result = build_row(meta, item.display_name, image_path, srs_label, oriented_imagery_type, i, pixels_normalized=True)
        if result.row is None:
            file_warnings.append({'filename': item.display_name, 'warning': '; '.join(result.warnings) or 'excluded'})
            continue
        rows.append(result.row)
        if result.warnings:
            file_warnings.append({'filename': item.display_name, 'warning': '; '.join(result.warnings)})

        manifest_entries.append({
            'original_filename': item.display_name,
            'derivative_filename': deriv_name,
            'source_sha256': _sha256_file(item.source_path),
            'derivative_sha256': _sha256_bytes(derivative_bytes),
            'status': result.status,
            'warnings': result.warnings,
        })

    csv_text = write_oriented_imagery_csv(rows)
    manifest = {
        'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'export_name': export_name,
        'oriented_imagery_type': oriented_imagery_type,
        'srs': srs_label,
        'esri_schema_reference': ESRI_DOC_URL,
        'metadata_source': 'generic_exif',
        'row_count': len(rows),
        'images': manifest_entries,
        'excluded': file_warnings,
    }
    readme = (
        'Esri Oriented Imagery portable package\n'
        '=======================================\n\n'
        f'Schema reference: {ESRI_DOC_URL}\n\n'
        'oriented_imagery.csv is the Oriented Imagery table. images/ holds\n'
        'orientation-normalized JPEG derivatives with EXIF, XMP, GPS, thumbnail,\n'
        'and device-serial metadata stripped -- the CSV carries the authoritative\n'
        'geolocation and camera fields instead. manifest.json records provenance\n'
        '(original/derivative filenames and SHA-256 digests) and any per-image\n'
        'warnings.\n\n'
        'Different cameras expose different metadata. Missing values are left\n'
        'blank and are not inferred.\n\n'
        'In ArcGIS Pro: Add Data > Oriented Imagery > From Table, and point it at\n'
        'oriented_imagery.csv in this folder.\n'
    )

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('oriented_imagery.csv', csv_text)
        zf.writestr('manifest.json', json.dumps(manifest, indent=2))
        zf.writestr('README.txt', readme)
        for name, data in images.items():
            zf.writestr(f'images/{name}', data)

    zip_bytes = zip_buf.getvalue()
    if len(zip_bytes) > MAX_PORTABLE_ZIP_BYTES:
        raise PortablePackageTooLarge(
            f'Portable package exceeds the {MAX_PORTABLE_ZIP_BYTES // (1024 * 1024)} MB limit.'
        )
    return zip_bytes
