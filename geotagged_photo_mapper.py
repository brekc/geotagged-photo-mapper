"""FastAPI backend for upload/EXIF extraction, isolated upload sessions,
CRS lookup and State Plane reference data, standard GIS exports, and
Oriented Imagery reference/portable exports.

Persistent writes are limited to State Plane source/cache files and PROJ
grid files under data/. Request-scoped upload/export files are created in
temporary directories and removed after each request.
"""

import base64
import io
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import urllib.request
import uuid
import zipfile
from typing import List
from urllib.parse import quote

import numpy as np
import pandas as pd
import shapely
from exiftool import ExifToolHelper
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps
from shapely.geometry import Point

import pillow_heif
from features import upload_sessions
from features.oriented_imagery import csv_safe_text

# Must run before any Image.open() call so .heic/.heif decode like any other format.
pillow_heif.register_heif_opener()

# Set PROJ grid cache before importing. This will preserve the grid cache
# following container restarts.
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
os.environ.setdefault('PROJ_USER_WRITABLE_DIRECTORY', os.path.join(_DATA_DIR, 'proj_cache'))

# PROJ_LIB and PROJ_DATA can point to an incompatible system copy from a
# PostgreSQL/PostGIS installation. Clear these to match the environment.
os.environ.pop('PROJ_LIB', None)
os.environ.pop('PROJ_DATA', None)

# These imports must come after the PROJ_LIB/PROJ_DATA cleanup above.
import geopandas as gpd  # noqa: E402
from pyproj import CRS  # noqa: E402
from pyproj.database import query_crs_info  # noqa: E402
from pyproj.enums import PJType  # noqa: E402
from pyproj.network import set_network_enabled  # noqa: E402

# Allow PROJ to download and cache the latest shift-grid files for
# datum transformations (e.g. NAD83(HARN) -> NAD83(2011))
set_network_enabled(True)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Load all projected EPSG CRS entries for /crs-search filtering.
try:
    _ALL_PROJECTED_CRS = list(query_crs_info(
        auth_name='EPSG',
        pj_types=PJType.PROJECTED_CRS,
        allow_deprecated=False,
    ))
except Exception:
    _ALL_PROJECTED_CRS = []

_SP_CSV_URL = 'https://raw.githubusercontent.com/ret3/stateplane/master/state_plane_reference.csv'
_COUNTIES_URL = 'https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_20m.zip'
_sp_zones_cache: dict | None = None
_sp_lock = threading.Lock()

# PROJ can return infinite coordinates when a datum-shift grid is still being
# fetched (see set_network_enabled above), so the reprojection is checked and
# retried instead of trusted.
_SP_REPROJECT_ATTEMPTS = 3
_SP_REPROJECT_RETRY_DELAY = 1.0


class StatePlaneUnavailable(Exception):
    pass


def _all_finite(node) -> bool:
    if isinstance(node, (list, tuple)):
        return len(node) > 0 and all(_all_finite(n) for n in node)
    return isinstance(node, (int, float)) and not isinstance(node, bool) and math.isfinite(node)


# A usable State Plane cache is a non-empty FeatureCollection whose every
# coordinate is a finite number. json.load() happily accepts Infinity/NaN, so
# this has to be checked explicitly.
def _valid_sp_geojson(data) -> bool:
    if not isinstance(data, dict) or data.get('type') != 'FeatureCollection':
        return False
    features = data.get('features')
    if not isinstance(features, list) or not features:
        return False
    for feature in features:
        if not isinstance(feature, dict):
            return False
        props = feature.get('properties')
        if not isinstance(props, dict) or props.get('epsg') is None or not props.get('name'):
            return False
        geometry = feature.get('geometry')
        if not isinstance(geometry, dict) or not _all_finite(geometry.get('coordinates')):
            return False
    return True


def _read_sp_cache(cache_path: str) -> dict | None:
    try:
        with open(cache_path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if _valid_sp_geojson(data) else None


# Write to a temp file in the same directory and swap it in, so a crash or a
# concurrent reader never sees a half-written cache.
def _write_sp_cache(cache_path: str, data: dict) -> None:
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(cache_path), prefix='.state_plane_', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, cache_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


# Download to a .part file first so an interrupted transfer never leaves a
# truncated file that later looks like a valid cached input.
def _download(url: str, dest: str) -> None:
    part = dest + '.part'
    try:
        urllib.request.urlretrieve(url, part)
        os.replace(part, dest)
    finally:
        if os.path.exists(part):
            os.remove(part)


def _zones_are_finite(gdf) -> bool:
    if len(gdf) == 0 or gdf.geometry.isna().any() or gdf.geometry.is_empty.any():
        return False
    return bool(np.isfinite(gdf.geometry.bounds.to_numpy()).all())


# The zone outlines are only a map overlay, so they do not need a sub-metre
# datum shift. Reprojecting with a grid-based NAD83 -> WGS 84 operation makes
# PROJ fetch datum grids over the network mid-transform; on a cold cache that
# fetch fails inside the server and the transform silently yields infinite
# coordinates. So this layer uses PROJ's grid-free operation (about 1-4 m,
# invisible at map scale). PROJ networking itself stays enabled for exports.
def _reproject_zones_to_wgs84(zones_gdf):
    from pyproj.transformer import TransformerGroup  # local: keeps the protected PROJ import block untouched

    group = TransformerGroup(zones_gdf.crs, 'EPSG:4326', always_xy=True)

    def _needs_grid(transformer) -> bool:
        ops = transformer.operations
        if ops:
            return any(op.grids for op in ops)
        return 'grid' in transformer.definition

    gridless = [t for t in group.transformers if not _needs_grid(t)]
    if not gridless:
        return zones_gdf.to_crs('EPSG:4326')
    transformer = gridless[0]
    geoms = np.array(list(zones_gdf.geometry), dtype=object)
    projected = shapely.transform(geoms, lambda c: np.column_stack(transformer.transform(c[:, 0], c[:, 1])))
    return zones_gdf.set_geometry(gpd.GeoSeries(projected, index=zones_gdf.index, crs='EPSG:4326'))


def _reproject_zones_checked(zones_gdf):
    for attempt in range(_SP_REPROJECT_ATTEMPTS):
        projected = _reproject_zones_to_wgs84(zones_gdf)
        if _zones_are_finite(projected):
            return projected
        if attempt < _SP_REPROJECT_ATTEMPTS - 1:
            time.sleep(_SP_REPROJECT_RETRY_DELAY)
    raise StatePlaneUnavailable(
        'State Plane zone reprojection produced invalid coordinates (PROJ may still be '
        'fetching datum-shift grids). Nothing was cached; try again shortly.'
    )


# Build US State Plane zones by joining a state plane reference CSV and the
# Census Bureau's county boundaries. Dissolving by zone and caching the result
# will keep the NAD83 zones as a reference layer. Raises StatePlaneUnavailable
# (and caches nothing) if the inputs cannot be fetched or the result is invalid.
def _build_sp_zones(cache_path: str) -> dict:
    try:
        return _build_sp_zones_unchecked(cache_path)
    except StatePlaneUnavailable:
        raise
    except Exception as e:
        # Generic on purpose: the raw error can contain server filesystem paths.
        raise StatePlaneUnavailable(
            f'State Plane zones could not be generated ({type(e).__name__}). '
            'Check the server\'s internet access and try again.'
        ) from e


def _build_sp_zones_unchecked(cache_path: str) -> dict:
    os.makedirs(_DATA_DIR, exist_ok=True)

    csv_path = os.path.join(_DATA_DIR, 'state_plane_reference.csv')
    if not os.path.exists(csv_path):
        _download(_SP_CSV_URL, csv_path)

    counties_dir = os.path.join(_DATA_DIR, 'counties_20m')
    if not (os.path.isdir(counties_dir) and any(f.endswith('.shp') for f in os.listdir(counties_dir))):
        zip_path = os.path.join(_DATA_DIR, 'cb_2023_us_county_20m.zip')
        _download(_COUNTIES_URL, zip_path)
        os.makedirs(counties_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(counties_dir)

    # Only need FIPS code and geometry for the zone join.
    shp_files = [f for f in os.listdir(counties_dir) if f.endswith('.shp')]
    counties_gdf = gpd.read_file(os.path.join(counties_dir, shp_files[0]))[['GEOID', 'geometry']]
    counties_gdf = counties_gdf.rename(columns={'GEOID': 'fips'})

    sp_df = pd.read_csv(csv_path)
    sp_df = sp_df[sp_df['status'] == 'current'].copy()
    sp_df['fips'] = sp_df['fips'].astype(str).str.zfill(5)
    sp_df = (
        sp_df[['fips', 'nad83_zone', 'nad83_epsg']]
        .dropna(subset=['nad83_epsg'])
        .drop_duplicates('fips')
    )
    sp_df['nad83_epsg'] = sp_df['nad83_epsg'].astype(int)

    # Dissolve counties into one polygon per zone.
    merged = counties_gdf.merge(sp_df, on='fips', how='inner')
    zones_gdf = merged.dissolve(by='nad83_epsg').reset_index()
    zones_gdf = _reproject_zones_checked(zones_gdf)

    # Need human-readable name and area-of-use for map popups.
    def _crs_info(epsg: int):
        try:
            crs = CRS.from_epsg(epsg)
            area = crs.area_of_use.name if crs.area_of_use else ''
            return crs.name, area
        except Exception:
            return str(epsg), ''

    zones_gdf[['name', 'area']] = zones_gdf['nad83_epsg'].apply(
        lambda e: pd.Series(_crs_info(e))
    )
    zones_gdf = zones_gdf[['nad83_epsg', 'name', 'area', 'geometry']].rename(
        columns={'nad83_epsg': 'epsg'}
    )

    result = json.loads(zones_gdf.to_json())
    if not _valid_sp_geojson(result):
        raise StatePlaneUnavailable('State Plane zone output failed validation; nothing was cached.')
    _write_sp_cache(cache_path, result)
    return result


# Return State Plane zone GeoJSON using, in order, the in-memory cache,
# the validated on-disk cache, or a full rebuild. An unreadable or
# non-finite on-disk cache is ignored and rebuilt.
def _get_sp_zones() -> dict:
    global _sp_zones_cache
    if _sp_zones_cache is not None:
        return _sp_zones_cache
    with _sp_lock:
        if _sp_zones_cache is not None:
            return _sp_zones_cache
        cache_path = os.path.join(_DATA_DIR, 'state_plane_zones.geojson')
        data = _read_sp_cache(cache_path)
        if data is None:
            data = _build_sp_zones(cache_path)
        _sp_zones_cache = data
        return data


# Expand two-letter state and province codes to full names for CRS area-of-use matching.
STATE_ABBR: dict[str, str] = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
    'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
    'WI': 'Wisconsin', 'WY': 'Wyoming', 'DC': 'District of Columbia',
    # Canadian provinces/territories
    'AB': 'Alberta', 'BC': 'British Columbia', 'MB': 'Manitoba', 'NB': 'New Brunswick',
    'NL': 'Newfoundland', 'NS': 'Nova Scotia', 'NT': 'Northwest Territories',
    'NU': 'Nunavut', 'ON': 'Ontario', 'PE': 'Prince Edward Island',
    'QC': 'Quebec', 'SK': 'Saskatchewan', 'YT': 'Yukon',
}

# ---------------------------------------------------------------------------
# Upload validation, limits, and filename safety
# ---------------------------------------------------------------------------

ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.heic', '.heif'}
HEIC_EXTENSIONS = {'.heic', '.heif'}

# Informational only -- the extension allow-list plus Pillow/ExifTool actually
# opening the file are the real gates. Blank/octet-stream are accepted since
# many browsers/OSes send no useful Content-Type for HEIC/HEIF.
ALLOWED_CONTENT_TYPES = {
    '', 'application/octet-stream',
    'image/jpeg', 'image/jpg', 'image/pjpeg',
    'image/png',
    'image/heic', 'image/heif',
    'image/heic-sequence', 'image/heif-sequence',
}

MAX_FILES_PER_UPLOAD = 60
MAX_FILE_SIZE_BYTES = 40 * 1024 * 1024
MAX_TOTAL_UPLOAD_BYTES = 400 * 1024 * 1024
MAX_DECODED_PIXELS = 60_000_000
UPLOAD_CHUNK_SIZE = 1024 * 1024
PREVIEW_MAX_DIMENSION = 1600

# Guard against decompression-bomb uploads: Pillow raises DecompressionBombError
# above this pixel count instead of silently decoding an oversized image.
Image.MAX_IMAGE_PIXELS = MAX_DECODED_PIXELS


def _safe_component(text: str) -> str:
    text = re.sub(r'[^A-Za-z0-9._-]', '_', text)
    return text.strip('._')


def _sanitized_disk_filename(original_name: str, ext: str) -> str:
    # Only for the on-disk temp filename -- never trust a client name as a
    # server path. The browser-facing/export name is tracked separately
    # (see _display_filename).
    base = os.path.basename((original_name or '').replace('\\', '/'))
    root = _safe_component(os.path.splitext(base)[0]) or 'photo'
    return f'{root[:100]}{ext}'


def _display_filename(original_name: str) -> str:
    base = os.path.basename((original_name or 'photo').replace('\\', '/'))
    base = ''.join(ch for ch in base if ch.isprintable())
    return base[:200] or 'photo'


_MAX_EXPORT_NAME_LENGTH = 100
_WINDOWS_RESERVED_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
}


# The one sanitizer for every user-supplied export name: it feeds both the
# on-disk/layer names and the Content-Disposition header. Strips control
# characters (CR, LF, NUL, U+2028...), drive prefixes, path separators and
# traversal segments, replaces quotes, semicolons and other characters that
# are unsafe in a header or on Windows, and bounds the length. Anything that
# ends up empty falls back to `fallback`.
def _safe_export_name(raw: str | None, fallback: str) -> str:
    text = unicodedata.normalize('NFC', raw or '')
    text = ''.join(
        ch for ch in text
        if unicodedata.category(ch)[0] != 'C' and unicodedata.category(ch) not in ('Zl', 'Zp')
    )
    text = re.sub(r'^\s*[A-Za-z]:', '', text.replace('\\', '/'))
    segments = [seg for seg in text.split('/') if seg.strip() and seg.strip() not in ('.', '..')]
    text = segments[-1] if segments else ''
    text = re.sub(r'["\';:*?<>|%]', '_', text)
    text = text.strip(' ._')[:_MAX_EXPORT_NAME_LENGTH].strip(' ._')
    if not text or set(text) <= {'_'}:
        return fallback
    if text.split('.')[0].upper() in _WINDOWS_RESERVED_NAMES:
        text = '_' + text
    return text


# Content-Disposition for a download. Non-ASCII names get an ASCII fallback
# plus an RFC 5987 filename* so the header itself is always valid latin-1.
def _attachment_headers(stem: str, suffix: str, extra: dict | None = None) -> dict:
    filename = f'{stem}{suffix}'
    try:
        filename.encode('ascii')
        disposition = f'attachment; filename="{filename}"'
    except UnicodeEncodeError:
        fallback = filename.encode('ascii', 'replace').decode('ascii').replace('?', '_')
        disposition = f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}'
    headers = {'Content-Disposition': disposition}
    if extra:
        headers.update(extra)
    return headers


# Neutralize spreadsheet formula injection in text cells of a DataFrame before
# CSV export. Only real strings are touched, so numeric columns (including
# negative coordinates) stay numeric.
def _csv_safe_frame(df):
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == object or pd.api.types.is_string_dtype(df[col]):
            df[col] = df[col].map(lambda v: csv_safe_text(v) if isinstance(v, str) else v)
    return df


def _validate_upload_extension(filename: str, content_type: str | None) -> str:
    ext = os.path.splitext(filename or '')[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f'Unsupported file type: {ext or "unknown"}')
    ct = (content_type or '').lower().split(';')[0].strip()
    if ct and ct not in ALLOWED_CONTENT_TYPES and not ct.startswith('image/'):
        raise ValueError(f'Unsupported content type: {content_type}')
    return ext


async def _stream_upload_to_file(upload: UploadFile, dest_path: str, max_bytes: int) -> int:
    total = 0
    with open(dest_path, 'wb') as out:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f'File exceeds the {max_bytes // (1024 * 1024)} MB size limit')
            out.write(chunk)
    return total


def _build_heic_preview(path: str) -> str | None:
    # Browsers can't render HEIC/HEIF inline, so build a bounded JPEG preview
    # server-side. JPEG/PNG stay client-side (see photoURLs in the frontend).
    try:
        with Image.open(path) as img:
            img.load()
            img = ImageOps.exif_transpose(img)  # the only pixel rotation applied -- avoids double rotation
            img = img.convert('RGB')
            img.thumbnail((PREVIEW_MAX_DIMENSION, PREVIEW_MAX_DIMENSION))
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=78)
    except Exception:
        return None
    return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')

# ---------------------------------------------------------------------------
# GPS and camera metadata extraction
# ---------------------------------------------------------------------------


# Return the first non-None value. Safer than `or` since 0.0 is valid.
def _coalesce(*values):
    for v in values:
        if v is not None:
            return v
    return None


def _altitude_below_sea_level(alt_ref) -> bool:
    if alt_ref is None:
        return False
    if isinstance(alt_ref, (int, float)):
        return int(alt_ref) == 1
    return str(alt_ref).strip().lower().startswith('below')


# GPSImgDirection is only a true heading when its Ref tag confirms it; a
# magnetic heading needs a declination correction this app doesn't attempt,
# so it's reported separately instead of used as-is.
def _heading_from_meta(meta: dict) -> tuple[float | None, bool]:
    heading = meta.get('EXIF:GPSImgDirection')
    if heading is None:
        return None, False
    try:
        heading = float(heading)
    except (TypeError, ValueError):
        return None, False
    ref = str(meta.get('EXIF:GPSImgDirectionRef') or '').strip().upper()
    return heading, ref == 'T'


VENDOR_POSE_TAGS = (
    'XMP:GimbalYawDegree', 'XMP:GimbalPitchDegree', 'XMP:GimbalRollDegree',
    'XMP:FlightYawDegree', 'XMP:FlightPitchDegree', 'XMP:FlightRollDegree',
)


def _as_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_int(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# Extract GPS and camera metadata via ExifTool. Returns (features, errors)
# rather than raising on a bad photo, so one unreadable/missing-GPS file
# never drops the rest of the batch.
#
# `upload_indexes[i]` is the position of file_paths[i] in the original upload.
# It is stamped on each feature as `upload_index`, so photos that share a
# filename can still be told apart and matched back to their uploaded file.
def extract_gps(file_paths, display_names: list[str] | None = None, upload_indexes: list[int] | None = None):
    display_names = display_names or []
    features = []
    errors = []
    with ExifToolHelper() as et:
        metadata_list = et.get_metadata(file_paths)
    for i, meta in enumerate(metadata_list):
        filename = display_names[i] if i < len(display_names) else os.path.basename(meta.get('SourceFile', ''))
        index_fields = {'upload_index': upload_indexes[i]} if upload_indexes and i < len(upload_indexes) else {}

        et_error = meta.get('ExifTool:Error')
        if et_error:
            errors.append({'filename': filename, 'error': f'Unreadable metadata: {et_error}', **index_fields})
            continue

        # Prefer Composite tags (signed decimal degrees); fall back to raw EXIF.
        composite_lat = meta.get('Composite:GPSLatitude')
        composite_lon = meta.get('Composite:GPSLongitude')
        lat = _coalesce(composite_lat, meta.get('EXIF:GPSLatitude'))
        lon = _coalesce(composite_lon, meta.get('EXIF:GPSLongitude'))

        if lat is None or lon is None:
            errors.append({'filename': filename, 'error': 'No GPS data found in this photo.', **index_fields})
            continue

        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            errors.append({'filename': filename, 'error': 'Unreadable GPS coordinates.', **index_fields})
            continue

        # Raw EXIF tags are unsigned; apply Ref tag sign if used.
        if composite_lat is None and str(meta.get('EXIF:GPSLatitudeRef', '')).upper() == 'S':
            lat = -abs(lat)
        if composite_lon is None and str(meta.get('EXIF:GPSLongitudeRef', '')).upper() == 'W':
            lon = -abs(lon)

        if not (math.isfinite(lat) and math.isfinite(lon)) or not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            errors.append({'filename': filename, 'error': 'GPS coordinates out of range.', **index_fields})
            continue

        composite_alt = meta.get('Composite:GPSAltitude')
        alt_raw = _coalesce(composite_alt, meta.get('EXIF:GPSAltitude'))
        altitude_m = _as_float(alt_raw)
        if altitude_m is not None and composite_alt is None and _altitude_below_sea_level(meta.get('EXIF:GPSAltitudeRef')):
            altitude_m = -abs(altitude_m)
        altitude_ft = round(altitude_m * 3.28084, 1) if altitude_m is not None else None

        heading_deg, heading_is_true = _heading_from_meta(meta)

        features.append({
            'filename': filename,
            **index_fields,
            'latitude': lat,
            'longitude': lon,
            'altitude_m': altitude_m,
            'altitude_ft': altitude_ft,
            'datetime': meta.get('EXIF:DateTimeOriginal'),
            'camera_model': meta.get('EXIF:Model'),
            'camera_make': meta.get('EXIF:Make'),
            'heading_deg': heading_deg,
            'heading_is_true': heading_is_true,
            'focal_length_mm': _as_float(meta.get('EXIF:FocalLength')),
            'focal_length_35mm_eq': _as_float(meta.get('EXIF:FocalLengthIn35mmFormat')),
            'orientation': _as_int(meta.get('EXIF:Orientation')),
            'pixel_width': _as_int(_coalesce(meta.get('EXIF:ExifImageWidth'), meta.get('File:ImageWidth'))),
            'pixel_height': _as_int(_coalesce(meta.get('EXIF:ExifImageHeight'), meta.get('File:ImageHeight'))),
            'subsec_time_original': meta.get('EXIF:SubSecTimeOriginal'),
            'offset_time_original': meta.get('EXIF:OffsetTimeOriginal'),
            'vendor_pose_detected': any(meta.get(tag) is not None for tag in VENDOR_POSE_TAGS),
        })

    return features, errors

# ---------------------------------------------------------------------------
# GeoJSON builder
# ---------------------------------------------------------------------------


# Convert extract_gps() dicts to a GeoJSON FeatureCollection string. Lat and Lon
# will become point geometry, and the remaining fields will become properties
# the frontend reads to build popups.
def build_geojson(features):
    geometries = [Point(f['longitude'], f['latitude']) for f in features]
    properties = [
        {k: v for k, v in f.items() if k not in ('latitude', 'longitude')}
        for f in features
    ]
    gdf = gpd.GeoDataFrame(properties, geometry=geometries, crs='EPSG:4326')
    return gdf.to_json()

# ---------------------------------------------------------------------------
# Custom CRS parsing
# ---------------------------------------------------------------------------


# Parse a WKT, PROJ4, or authority string into a CRS. Falls back to from_wkt() for
# ESRI .prj files that from_user_input() can not classify.
def _parse_custom_crs(text: str) -> CRS:
    text = text.strip()
    try:
        return CRS.from_user_input(text)
    except Exception:
        pass
    try:
        return CRS.from_wkt(text)
    except Exception:
        raise ValueError(
            'Could not parse the custom CRS. Paste a valid WKT or PROJ4 string, '
            'or upload a .prj file that contains one.'
        )


def _resolve_target_crs(epsg: str, custom_crs: str) -> CRS:
    custom_crs = (custom_crs or '').strip()
    if custom_crs:
        try:
            return _parse_custom_crs(custom_crs)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if epsg.strip():
        try:
            return CRS.from_epsg(int(epsg))
        except Exception:
            raise HTTPException(status_code=400, detail=f'Invalid EPSG code: {epsg}')
    raise HTTPException(status_code=400, detail='No EPSG code or custom CRS provided')


# Only an exact EPSG match is reported by code; a looser guess could label
# coordinates with a CRS they are not actually in, so anything else is
# written as WKT.
def _srs_label(target_crs: CRS) -> str:
    epsg = target_crs.to_epsg(min_confidence=100)
    return str(epsg) if epsg is not None else target_crs.to_wkt()


# Transform each row's WGS 84 longitude/latitude into the target CRS through
# the same GeoPandas/PROJ path the standard exports use. Returns copies whose
# `longitude`/`latitude` hold the target CRS's X/Y (so Oriented Imagery X/Y
# always agree with its SRS). A row whose result is not finite is flagged with
# `reprojection_failed` and its coordinates blanked instead of exported.
def _reproject_rows(rows: list[dict], target_crs: CRS) -> list[dict]:
    if not rows:
        return []
    geometries = [Point(r['longitude'], r['latitude']) for r in rows]
    projected = gpd.GeoDataFrame({'_i': range(len(rows))}, geometry=geometries, crs='EPSG:4326').to_crs(target_crs)
    out = []
    for row, geom in zip(rows, projected.geometry):
        new_row = dict(row)
        x, y = (geom.x, geom.y) if geom is not None and not geom.is_empty else (float('nan'), float('nan'))
        if math.isfinite(x) and math.isfinite(y):
            new_row['longitude'], new_row['latitude'] = x, y
        else:
            new_row['longitude'] = new_row['latitude'] = None
            new_row['reprojection_failed'] = True
        out.append(new_row)
    return out


def _rows_geodataframe(rows: list[dict]):
    geometries = [Point(f['longitude'], f['latitude']) for f in rows]
    properties = [
        # latitude/longitude become the geometry; photo_id and upload_index are
        # session-scoped bookkeeping with no meaning once the file is downloaded.
        {k: v for k, v in f.items() if k not in ('latitude', 'longitude', 'photo_id', 'upload_index')}
        for f in rows
    ]
    return gpd.GeoDataFrame(properties, geometry=geometries, crs='EPSG:4326')


def _parse_photo_ids(raw: str | None) -> list[str] | None:
    if raw is None or raw.strip() == '':
        return None
    return [r for r in (part.strip() for part in raw.split(',')) if r]


def _get_session_rows(upload_id: str, photo_ids_raw: str | None) -> list[dict]:
    try:
        rows = upload_sessions.get_rows(upload_id, _parse_photo_ids(photo_ids_raw))
    except upload_sessions.SessionNotFound:
        raise HTTPException(status_code=404, detail='Unknown or expired upload session. Upload photos again.')
    if not rows:
        raise HTTPException(status_code=400, detail='No data to export, upload photos first')
    return rows

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get('/')
async def index(request: Request):
    return templates.TemplateResponse(request, 'geotagged-photo-mapper.html')


@app.post('/upload')
async def upload(
    request: Request,
    photos: List[UploadFile] = File(...),
):
    # Temp files are needed because ExifTool requires real file paths.
    # Results land in a fresh, isolated upload session for /export and
    # Oriented Imagery to use.
    if not photos:
        raise HTTPException(status_code=400, detail='No files received')
    if len(photos) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(status_code=400, detail=f'Too many files in one upload (max {MAX_FILES_PER_UPLOAD})')

    content_length = request.headers.get('content-length')
    if content_length is not None:
        try:
            if int(content_length) > MAX_TOTAL_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f'Upload exceeds the {MAX_TOTAL_UPLOAD_BYTES // (1024 * 1024)} MB request limit',
                )
        except ValueError:
            pass

    tmp_dir = tempfile.mkdtemp()
    try:
        saved_paths = []
        display_names = []
        upload_indexes = []
        preview_by_index: dict[int, str] = {}
        errors = []
        total_bytes = 0

        # A filename is not unique (two folders can each hold IMG_0001.JPG), so
        # each file is identified by its position in this upload.
        for upload_index, f in enumerate(photos):
            original_name = f.filename or 'photo'
            display_name = _display_filename(original_name)
            try:
                ext = _validate_upload_extension(original_name, f.content_type)

                remaining = MAX_TOTAL_UPLOAD_BYTES - total_bytes
                if remaining <= 0:
                    raise ValueError('Aggregate upload size limit exceeded')

                sub_dir = os.path.join(tmp_dir, uuid.uuid4().hex)
                os.makedirs(sub_dir)
                dest = os.path.join(sub_dir, _sanitized_disk_filename(original_name, ext))

                size = await _stream_upload_to_file(f, dest, min(MAX_FILE_SIZE_BYTES, remaining))
                total_bytes += size

                saved_paths.append(dest)
                display_names.append(display_name)
                upload_indexes.append(upload_index)

                if ext in HEIC_EXTENSIONS:
                    preview = _build_heic_preview(dest)
                    if preview:
                        preview_by_index[upload_index] = preview
                    else:
                        errors.append({
                            'filename': display_name,
                            'error': 'Could not generate a preview for this HEIC/HEIF photo.',
                        })
            except ValueError as e:
                errors.append({'filename': display_name, 'error': str(e)})
            finally:
                await f.close()

        if saved_paths:
            features, gps_errors = extract_gps(saved_paths, display_names, upload_indexes)
            errors.extend(gps_errors)
        else:
            features = []

        upload_id = upload_sessions.create_session()
        try:
            stored_rows = upload_sessions.set_rows(upload_id, features)
        except upload_sessions.SessionLimitExceeded as e:
            upload_sessions.delete_session(upload_id)
            raise HTTPException(status_code=429, detail=str(e))

        # The GeoJSON is built from the stored rows, so every feature carries
        # its own photo_id (and upload_index) with no filename-based lookup.
        geojson_obj = json.loads(build_geojson(stored_rows)) if stored_rows else {
            'type': 'FeatureCollection', 'features': [],
        }
        # Previews are keyed by photo_id: photos that share a filename each keep their own.
        previews = {
            r['photo_id']: preview_by_index[r['upload_index']]
            for r in stored_rows if r['upload_index'] in preview_by_index
        }

        return {
            'upload_id': upload_id,
            'geojson': geojson_obj,
            'total_uploaded': len(photos),
            'total_geotagged': len(features),
            'previews': previews,
            'errors': errors,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.delete('/session/{upload_id}')
async def delete_session(upload_id: str):
    # Idempotent: deleting an unknown/already-deleted/expired id still
    # returns success, matching "Clear All" and best-effort unload cleanup.
    upload_sessions.delete_session(upload_id)
    return {'deleted': True}


@app.post('/session/{upload_id}/close')
async def close_session(upload_id: str):
    # POST alias of the DELETE above: navigator.sendBeacon() can only send
    # POST, so this is what the browser calls on page unload for best-effort
    # cleanup (the 15-minute TTL is the fallback if this never arrives).
    upload_sessions.delete_session(upload_id)
    return {'deleted': True}


@app.get('/zone-geojson')
# Return State Plane zone polygons. UTM zones are generated through the frontend
# with buildUtmLayer().
def zone_geojson(zone_type: str = Query(..., alias='type')):
    if zone_type != 'state_plane':
        raise HTTPException(status_code=400, detail='type must be state_plane')
    try:
        return _get_sp_zones()
    except StatePlaneUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get('/crs-search')
# Search projected CRS entries by area-of-use name for the Region dropdown.
async def crs_search(q: str = Query(default='')):
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail='Query must be at least 2 characters')
    # Expand abbreviations.
    term = STATE_ABBR.get(q.upper(), q)

    escaped = re.escape(term)
    # Match state-level areas and exclude county/parish/borough sub-matches.
    area_pattern = re.compile(
        rf'-\s+{escaped}(?!\s+(?:County|Parish|Borough|Municipality|Census\s+Area|Township))\b',
        re.IGNORECASE,
    )

    output = [
        {'code': int(r.code), 'name': r.name, 'area': r.area_of_use.name}
        for r in _ALL_PROJECTED_CRS
        if area_pattern.search(r.area_of_use.name or '')
    ]

    output.sort(key=lambda x: x['name'])
    return output[:400]


@app.post('/export')
# Reproject the current upload session's rows and return as a downloadable
# file. custom_crs will take priority over epsg. Requires the matching
# upload_id -- another session's data is never visible here.
async def export(
    format: str = Form(...),
    upload_id: str = Form(...),
    photo_ids: str = Form(default=''),
    epsg: str = Form(default=''),
    custom_crs: str = Form(default=''),
    source_path: str = Form(default=''),
    flight_altitude: str = Form(default=''),
    altitude_unit: str = Form(default='feet'),
    export_name: str = Form(default='photo_locations'),
):
    fmt = format.lower()

    # Sanitize for an internal layer name.
    name = _safe_export_name(export_name, 'photo_locations')

    target_crs = _resolve_target_crs(epsg, custom_crs)
    rows = _get_session_rows(upload_id, photo_ids)

    gdf = _rows_geodataframe(rows)

    # Add source if a path is provided.
    clean_source = source_path.strip()
    if clean_source:
        if not clean_source.endswith(('/', '\\')):
            sep = '\\' if '\\' in clean_source else '/'
            clean_source += sep
        gdf['source'] = gdf['filename'].apply(lambda fn: clean_source + fn)

    # Add altitude if provided.
    try:
        alt_val = float(flight_altitude) if flight_altitude.strip() else None
    except ValueError:
        alt_val = None

    if alt_val is not None:
        # Only write the entered units.
        if altitude_unit == 'meters':
            gdf['flight_alt_m'] = round(alt_val, 1)
        else:
            gdf['flight_alt_ft'] = round(alt_val, 1)

    gdf = gdf.to_crs(target_crs)

    tmp_dir = tempfile.mkdtemp()
    try:
        # Handlers for GIS file formats.
        if fmt == 'csv':
            csv_gdf = gdf.copy()
            # Label lat-lon for geographic CRS and easting-northing for projected.
            if target_crs.is_geographic:
                csv_gdf['longitude'] = csv_gdf.geometry.x
                csv_gdf['latitude'] = csv_gdf.geometry.y
            else:
                csv_gdf['easting'] = csv_gdf.geometry.x
                csv_gdf['northing'] = csv_gdf.geometry.y
            csv_gdf = _csv_safe_frame(csv_gdf.drop(columns='geometry'))
            buf = io.StringIO()
            csv_gdf.to_csv(buf, index=False)
            return Response(
                content=buf.getvalue().encode(),
                media_type='text/csv',
                headers=_attachment_headers(name, '.csv'),
            )

        elif fmt == 'filegdb':
            # FileGDBs are directories and need zipped for download.
            gdb_path = os.path.join(tmp_dir, f'{name}.gdb')
            gdf.to_file(gdb_path, driver='OpenFileGDB', layer=name)
            zip_path = os.path.join(tmp_dir, f'{name}_gdb.zip')
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(gdb_path):
                    for file in files:
                        abs_path = os.path.join(root, file)
                        arc_name = os.path.relpath(abs_path, tmp_dir)
                        zf.write(abs_path, arc_name)
            with open(zip_path, 'rb') as fh:
                content = fh.read()
            return Response(
                content=content,
                media_type='application/zip',
                headers=_attachment_headers(name, '_gdb.zip'),
            )

        elif fmt == 'geojson':
            out_path = os.path.join(tmp_dir, f'{name}.geojson')
            gdf.to_file(out_path, driver='GeoJSON')
            with open(out_path, 'rb') as fh:
                content = fh.read()
            return Response(
                content=content,
                media_type='application/geo+json',
                headers=_attachment_headers(name, '.geojson'),
            )

        elif fmt == 'geopackage':
            out_path = os.path.join(tmp_dir, f'{name}.gpkg')
            gdf.to_file(out_path, driver='GPKG', layer=name)
            with open(out_path, 'rb') as fh:
                content = fh.read()
            return Response(
                content=content,
                media_type='application/geopackage+sqlite3',
                headers=_attachment_headers(name, '.gpkg'),
            )

        elif fmt == 'kml':
            # KML requires WGS 84 coordinates and the LIBKML driver to maintain gdf formatting.
            kml_gdf = gdf.to_crs('EPSG:4326')
            kml_gdf = kml_gdf.copy()
            kml_gdf['Name'] = kml_gdf['filename']
            out_path = os.path.join(tmp_dir, f'{name}.kml')
            kml_gdf.to_file(out_path, driver='LIBKML')
            with open(out_path, 'rb') as fh:
                content = fh.read()
            return Response(
                content=content,
                media_type='application/vnd.google-earth.kml+xml',
                headers=_attachment_headers(name, '.kml'),
            )

        elif fmt == 'shapefile':
            # ZIP the shapefile and all supporting files.
            shp_dir = os.path.join(tmp_dir, 'shapefile')
            os.makedirs(shp_dir)
            shp_path = os.path.join(shp_dir, f'{name}.shp')
            gdf.to_file(shp_path, driver='ESRI Shapefile')
            zip_path = os.path.join(tmp_dir, f'{name}_shp.zip')
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for ext in ('.shp', '.shx', '.dbf', '.prj', '.cpg'):
                    candidate = os.path.join(shp_dir, f'{name}{ext}')
                    if os.path.exists(candidate):
                        zf.write(candidate, f'{name}{ext}')
            with open(zip_path, 'rb') as fh:
                content = fh.read()
            return Response(
                content=content,
                media_type='application/zip',
                headers=_attachment_headers(name, '_shp.zip'),
            )

        else:
            raise HTTPException(status_code=400, detail=f'Unknown format: {fmt}')

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post('/oriented-imagery/preflight')
# Per-file completeness counts for the Build Oriented Imagery panel, computed
# from the upload session -- no repost required at this stage.
async def oriented_imagery_preflight(
    upload_id: str = Form(...),
    photo_ids: str = Form(default=''),
    epsg: str = Form(default=''),
    custom_crs: str = Form(default=''),
    mode: str = Form(default=''),
):
    from features.oriented_imagery import build_preflight  # deferred: avoids a circular import at module load

    rows = _get_session_rows(upload_id, photo_ids)
    # With a CRS selected, count against the reprojected coordinates so a row
    # that cannot be transformed shows up as excluded here, not only in the export.
    if epsg.strip() or custom_crs.strip():
        rows = _reproject_rows(rows, _resolve_target_crs(epsg, custom_crs))
    return build_preflight(rows, reference_mode=(mode == 'reference'))


@app.post('/oriented-imagery/reference')
# Mode A: point ImagePath at images that already exist somewhere the *end
# user's* machine can read. Only oriented_imagery.csv is produced.
async def oriented_imagery_reference(
    upload_id: str = Form(...),
    photo_ids: str = Form(default=''),
    base_location: str = Form(...),
    oriented_imagery_type: str = Form(...),
    epsg: str = Form(default=''),
    custom_crs: str = Form(default=''),
    export_name: str = Form(default='oriented_imagery'),
):
    from features.oriented_imagery import ORIENTED_IMAGERY_TYPES, InvalidBaseLocation, build_reference_export

    if oriented_imagery_type not in ORIENTED_IMAGERY_TYPES:
        raise HTTPException(status_code=400, detail='OrientedImageryType must be explicitly selected.')

    target_crs = _resolve_target_crs(epsg, custom_crs)
    rows = _reproject_rows(_get_session_rows(upload_id, photo_ids), target_crs)

    try:
        result = build_reference_export(rows, base_location, _srs_label(target_crs), oriented_imagery_type)
    except InvalidBaseLocation as e:
        raise HTTPException(status_code=400, detail=str(e))

    name = _safe_export_name(export_name, 'oriented_imagery')
    return Response(
        content=result['csv'].encode('utf-8'),
        media_type='text/csv',
        headers=_attachment_headers(name, '.csv', {
            'X-Oriented-Imagery-Row-Count': str(result['row_count']),
            'X-Oriented-Imagery-Excluded-Count': str(result['excluded_count']),
        }),
    )


@app.post('/oriented-imagery/reference-preview')
# Preview resolved paths and warnings before the Mode A download happens.
async def oriented_imagery_reference_preview(
    upload_id: str = Form(...),
    photo_ids: str = Form(default=''),
    base_location: str = Form(...),
    oriented_imagery_type: str = Form(...),
    epsg: str = Form(default=''),
    custom_crs: str = Form(default=''),
):
    from features.oriented_imagery import ORIENTED_IMAGERY_TYPES, InvalidBaseLocation, build_reference_export

    if oriented_imagery_type not in ORIENTED_IMAGERY_TYPES:
        raise HTTPException(status_code=400, detail='OrientedImageryType must be explicitly selected.')

    target_crs = _resolve_target_crs(epsg, custom_crs)
    rows = _reproject_rows(_get_session_rows(upload_id, photo_ids), target_crs)

    try:
        result = build_reference_export(rows, base_location, _srs_label(target_crs), oriented_imagery_type)
    except InvalidBaseLocation as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        'preview_paths': result['preview_paths'],
        'row_count': result['row_count'],
        'excluded_count': result['excluded_count'],
        'warnings': result['warnings'],
        'note': result['note'],
    }


@app.post('/oriented-imagery/portable')
# Mode B: repost the currently-included photos, re-extract metadata fresh,
# convert to privacy-stripped orientation-normalized JPEG derivatives, and
# package a ZIP. The session never stores photo bytes, so a repost is required.
async def oriented_imagery_portable(
    request: Request,
    upload_id: str = Form(...),
    oriented_imagery_type: str = Form(...),
    photo_ids: str = Form(default=''),
    epsg: str = Form(default=''),
    custom_crs: str = Form(default=''),
    export_name: str = Form(default='oriented_imagery'),
    photos: List[UploadFile] = File(...),
):
    from features.oriented_imagery import (
        ORIENTED_IMAGERY_TYPES,
        PortableItem,
        PortablePackageTooLarge,
        build_portable_package,
    )

    if oriented_imagery_type not in ORIENTED_IMAGERY_TYPES:
        raise HTTPException(status_code=400, detail='OrientedImageryType must be explicitly selected.')
    if not photos:
        raise HTTPException(status_code=400, detail='No files received')
    if len(photos) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(status_code=400, detail=f'Too many files in one upload (max {MAX_FILES_PER_UPLOAD})')

    # upload_id confirms a live session (fails closed otherwise); the rows
    # themselves come from re-extracting the reposted files below.
    try:
        session_photo_ids = {r['photo_id'] for r in upload_sessions.get_rows(upload_id)}
    except upload_sessions.SessionNotFound:
        raise HTTPException(status_code=404, detail='Unknown or expired upload session. Upload photos again.')

    # photo_ids, when sent, is parallel to `photos`: entry N names the session
    # photo that file N is a repost of. That position (never the filename) is
    # what ties each file to its metadata, so duplicate filenames stay separate.
    repost_ids = _parse_photo_ids(photo_ids)
    if repost_ids is not None and len(repost_ids) != len(photos):
        raise HTTPException(status_code=400, detail='photo_ids must list one id per uploaded photo.')

    target_crs = _resolve_target_crs(epsg, custom_crs)
    name = _safe_export_name(export_name, 'oriented_imagery')

    tmp_dir = tempfile.mkdtemp()
    try:
        saved_paths = []
        display_names = []
        items = []
        total_bytes = 0

        upload_indexes = []

        for upload_index, f in enumerate(photos):
            original_name = f.filename or 'photo'
            display_name = _display_filename(original_name)
            try:
                if repost_ids is not None and repost_ids[upload_index] not in session_photo_ids:
                    raise ValueError('Photo is no longer part of this upload session')
                ext = _validate_upload_extension(original_name, f.content_type)
                remaining = MAX_TOTAL_UPLOAD_BYTES - total_bytes
                if remaining <= 0:
                    raise ValueError('Aggregate upload size limit exceeded')

                sub_dir = os.path.join(tmp_dir, uuid.uuid4().hex)
                os.makedirs(sub_dir)
                dest = os.path.join(sub_dir, _sanitized_disk_filename(original_name, ext))
                size = await _stream_upload_to_file(f, dest, min(MAX_FILE_SIZE_BYTES, remaining))
                total_bytes += size

                saved_paths.append(dest)
                display_names.append(display_name)
                upload_indexes.append(upload_index)
                items.append(PortableItem(display_name=display_name, source_path=dest, key=upload_index))
            except ValueError:
                continue  # unsupported/oversized files are simply excluded from the package
            finally:
                await f.close()

        if not saved_paths:
            raise HTTPException(status_code=400, detail='No supported photos were received.')

        rows_meta, _errors = extract_gps(saved_paths, display_names, upload_indexes)
        rows_meta = _reproject_rows(rows_meta, target_crs)

        try:
            zip_bytes = build_portable_package(
                items, rows_meta, _srs_label(target_crs), oriented_imagery_type, name,
            )
        except PortablePackageTooLarge as e:
            raise HTTPException(status_code=413, detail=str(e))

        return Response(
            content=zip_bytes,
            media_type='application/zip',
            headers=_attachment_headers(name, '.zip'),
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('geotagged_photo_mapper:app', host='0.0.0.0', port=8000)
