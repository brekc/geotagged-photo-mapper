"""Build reference and portable Esri Oriented Imagery exports."""

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
from urllib.parse import quote, urlsplit, urlunsplit

from PIL import ImageOps

from features.image_processing import open_checked_image
from features.standard_exports import csv_safe_text

ESRI_DOC_URL = 'https://doc.esri.com/en/arcgis-pro/latest/help/data/imagery/oriented-imagery-table.html'

# Table schema

# Documented Oriented Imagery table columns, in a stable order.
CORE_FIELDS = [
    'X', 'Y', 'ImagePath', 'SRS', 'Name', 'Z', 'AcquisitionDate',
    'CameraHeading', 'CameraPitch', 'CameraRoll',
    'HorizontalFieldOfView', 'VerticalFieldOfView', 'ImageRotation',
    'OrientedImageryType', 'Omega', 'Phi', 'Kappa', 'Matrix',
    'FocalLength', 'PrincipalX', 'PrincipalY', 'Radial', 'Tangential',
    'A0', 'A1', 'A2', 'B0', 'B1', 'B2', 'SequenceOrder',
]

# Audit/provenance columns this app adds after the core fields.
AUDIT_FIELDS = [
    'Make', 'Model', 'FocalLength35mmEq', 'MetadataSource',
    'OrientationStatus', 'RowWarnings',
]

ORIENTED_IMAGERY_FIELDS = CORE_FIELDS + AUDIT_FIELDS

# Escape attacker- or camera-controlled text fields against CSV formula
# injection. Keep numeric fields numeric, including negative coordinates.
_TEXT_FIELDS = {
    'ImagePath', 'SRS', 'Name', 'AcquisitionDate', 'OrientedImageryType',
    'Matrix', 'Make', 'Model', 'MetadataSource', 'OrientationStatus',
    'RowWarnings',
}

ORIENTED_IMAGERY_TYPES = ('Horizontal', 'Oblique', 'Nadir', '360', 'Inspection')

# Detected but never mapped to CameraHeading/Omega/Phi/Kappa: only a documented,
# fixture-tested adapter should translate vendor pose data. Detection here just
# triggers a warning (see build_row) instead of guessing a value.
VENDOR_POSE_TAGS = (
    'XMP:GimbalYawDegree', 'XMP:GimbalPitchDegree', 'XMP:GimbalRollDegree',
    'XMP:FlightYawDegree', 'XMP:FlightPitchDegree', 'XMP:FlightRollDegree',
)

# CSV safety


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


# Base-location (Mode A) validation

_ALLOWED_URL_SCHEMES = {'http', 'https'}
_MAX_BASE_LOCATION_LENGTH = 500
_CONTROL_CHAR_RE = re.compile(r'[\x00-\x1f\x7f]')
_WINDOWS_DRIVE_RE = re.compile(r'^[A-Za-z]:[\\/]')


class InvalidBaseLocation(ValueError):
    pass


# Validate and normalize a reference-mode path or URL without accessing it.
# The result is safe to write to CSV but may not resolve on the end user's
# machine; invalid values raise InvalidBaseLocation.
def validate_base_location(raw: str) -> tuple[str, str]:
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
        # The trailing slash belongs on the path, not after any ?query.
        path = parts.path if parts.path.endswith('/') else parts.path + '/'
        return 'url', urlunsplit((parts.scheme, parts.netloc, path, parts.query, ''))

    if text.startswith('\\\\'):
        # UNC paths are always backslash-separated.
        return 'unc', text.replace('/', '\\').rstrip('\\') + '\\'

    if _WINDOWS_DRIVE_RE.match(text):
        # Drive-letter paths are always backslash-separated, even if typed with forward slashes.
        return 'local', text.replace('/', '\\').rstrip('\\') + '\\'

    if text.startswith('/'):
        # POSIX paths are always forward-slash-separated.
        return 'local', text.rstrip('/') + '/'

    raise InvalidBaseLocation(
        'Enter an absolute local path (C:\\...), a UNC path (\\\\server\\share\\...), '
        'or an http(s):// URL.'
    )


# Join a validated base and filename with the correct separator; URL filenames
# are percent-encoded.
def join_reference_path(kind: str, normalized_base: str, name: str) -> str:
    if kind == 'url':
        parts = urlsplit(normalized_base)
        path = parts.path.rstrip('/') + '/' + quote(name, safe='')
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ''))
    sep = '\\' if kind == 'unc' or _WINDOWS_DRIVE_RE.match(normalized_base) else '/'
    return normalized_base.rstrip('\\/') + sep + name


MODE_A_ALLOWED_EXTENSIONS = {'.jpg', '.jpeg'}
MODE_A_WARN_EXTENSIONS = {'.png', '.heic', '.heif'}

# Row building


@dataclass
class RowResult:
    row: dict | None
    status: str  # complete | partial | minimum-only | excluded | error
    warnings: list[str] = field(default_factory=list)


# EXIF Orientation -> (rotation degrees to correct for display, is_mirrored).
# ImageRotation can't express a mirror, so mirrored orientations are reported
# via the warning list instead of guessing a value.
def _orientation_rotation_degrees(orientation) -> tuple[int | None, bool]:
    mapping = {1: (0, False), 3: (180, False), 6: (270, False), 8: (90, False),
               2: (0, True), 4: (180, True), 5: (270, True), 7: (90, True)}
    return mapping.get(orientation, (None, False))


# Estimate FOV from a 35mm-equivalent focal length and 36x24 mm frame. This is
# explicitly approximate, not a calibrated camera model.
def _estimate_fov(focal_35mm, width, height, orientation) -> tuple[float | None, float | None]:
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
    # Build one row from normalized extract_gps() metadata. In portable mode,
    # `pixels_normalized` means EXIF orientation is already baked into the JPEG.
    try:
        # `latitude`/`longitude` hold this row's X/Y already expressed in the
        # CRS named by srs_label (the caller reprojects before building rows).
        lat = meta.get('latitude')
        lon = meta.get('longitude')
        if meta.get('reprojection_failed'):
            return RowResult(row=None, status='excluded', warnings=['reprojection_failed'])
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


# Summarize metadata completeness for the preview panel. Reference-mode counts
# also include unsupported extensions and duplicate ImagePaths.
def build_preflight(rows_meta: list[dict], reference_mode: bool = False) -> dict:
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
    if reference_mode:
        excluded = build_reference_export(rows_meta, '/', '', 'Nadir')['excluded_count']
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


# Filenames


def _safe_stem(name: str) -> str:
    stem = os.path.splitext(os.path.basename(name or ''))[0]
    stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem).strip('._')
    return stem[:80] or 'photo'


# The sequence index keeps names deterministic and collision-safe when
# different originals sanitize to the same stem.
def derivative_filename(index: int, original_display_name: str) -> str:
    return f'{index:04d}_{_safe_stem(original_display_name)}.jpg'


# Mode A: reference existing images


def build_reference_export(
    rows_meta: list[dict],
    base_location: str,
    srs_label: str,
    oriented_imagery_type: str,
) -> dict:
    # Return the reference CSV, a short path preview, warnings, and counts.
    kind, normalized_base = validate_base_location(base_location)

    rows = []
    file_warnings = []
    excluded = 0
    seen_paths: set[str] = set()
    for i, meta in enumerate(rows_meta, start=1):
        display_name = meta.get('filename', f'photo_{i}')
        ext = os.path.splitext(display_name)[1].lower()

        if ext in MODE_A_WARN_EXTENSIONS:
            file_warnings.append({
                'filename': display_name,
                'warning': f'{ext} is not a documented source-image format for reference mode; excluded.',
            })
            excluded += 1
            continue
        if ext not in MODE_A_ALLOWED_EXTENSIONS:
            file_warnings.append({'filename': display_name, 'warning': f'Unsupported source format: {ext or "unknown"}'})
            excluded += 1
            continue

        clean_name = display_name.replace('\\', '').replace('/', '')
        image_path = join_reference_path(kind, normalized_base, clean_name)

        result = build_row(meta, display_name, image_path, srs_label, oriented_imagery_type, i, pixels_normalized=False)
        if result.row is None:
            file_warnings.append({'filename': display_name, 'warning': '; '.join(result.warnings) or 'excluded'})
            excluded += 1
            continue
        if image_path in seen_paths:
            # Only rows that get written claim a path, so a first row excluded
            # for another reason (e.g. no GPS) never shadows a later one.
            file_warnings.append({
                'filename': display_name,
                'warning': f'duplicate_image_path: {image_path} is already used by an earlier photo; excluded.',
            })
            excluded += 1
            continue
        seen_paths.add(image_path)
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


# Mode B: portable package

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


# Create an orientation-normalized JPEG without EXIF, XMP, GPS, thumbnail, or
# serial metadata. Preserve only a small, bounded ICC profile.
def _build_derivative_jpeg(source_path: str) -> bytes:
    with open_checked_image(source_path) as img:
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


# Match `key` to `upload_index` so duplicate filenames keep distinct metadata;
# unkeyed items fall back to display-name order.
@dataclass
class PortableItem:
    display_name: str
    source_path: str
    key: int | None = None


def build_portable_package(
    items: list[PortableItem],
    rows_meta: list[dict],
    srs_label: str,
    oriented_imagery_type: str,
    export_name: str,
) -> bytes:
    # Build the portable ZIP in memory and enforce its size limit. The caller
    # must delete request-scoped `items[*].source_path` files in a finally block.
    meta_by_key = {m['upload_index']: m for m in rows_meta if m.get('upload_index') is not None}
    unkeyed_by_name: dict[str, list[dict]] = {}
    for m in rows_meta:
        if m.get('upload_index') is None:
            unkeyed_by_name.setdefault(m.get('filename'), []).append(m)

    rows = []
    manifest_entries = []
    file_warnings = []
    total_bytes = 0
    images: dict[str, bytes] = {}

    for i, item in enumerate(items, start=1):
        if item.key is not None:
            meta = meta_by_key.get(item.key)
        else:
            candidates = unkeyed_by_name.get(item.display_name)
            meta = candidates.pop(0) if candidates else None
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
        'Oriented Imagery portable package\n'
        '==================================\n\n'
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
