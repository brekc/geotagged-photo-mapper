"""Build standard GIS exports and enforce download safety."""

import io
import os
import re
import unicodedata
import zipfile
from urllib.parse import quote

import pandas as pd

EXPORT_SUFFIXES = {
    'csv': '.csv',
    'filegdb': '_gdb.zip',
    'geojson': '.geojson',
    'geopackage': '.gpkg',
    'kml': '.kml',
    'shapefile': '_shp.zip',
}

EXPORT_MEDIA_TYPES = {
    'csv': 'text/csv',
    'filegdb': 'application/zip',
    'geojson': 'application/geo+json',
    'geopackage': 'application/geopackage+sqlite3',
    'kml': 'application/vnd.google-earth.kml+xml',
    'shapefile': 'application/zip',
}

_MAX_EXPORT_NAME_LENGTH = 100
_WINDOWS_RESERVED_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
}

_CSV_DANGEROUS_PREFIXES = ('=', '+', '-', '@', '\t', '\r', '\n')


# Protect text cells from CSV formula injection. Do not modify numeric fields,
# because values such as negative coordinates must remain numeric.
def csv_safe_text(value) -> str:
    if value is None:
        return ''
    text = str(value)
    if text.startswith(_CSV_DANGEROUS_PREFIXES):
        return "'" + text
    return text


# Sanitize every user-supplied export name once for layer names, disk paths,
# and Content-Disposition. Remove controls, path syntax, traversal, and
# Windows-unsafe characters; bound the length and fall back if nothing remains.
def safe_export_name(raw: str | None, fallback: str) -> str:
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
def attachment_headers(stem: str, suffix: str, extra: dict | None = None) -> dict:
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
def csv_safe_frame(df):
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == object or pd.api.types.is_string_dtype(df[col]):
            df[col] = df[col].map(lambda v: csv_safe_text(v) if isinstance(v, str) else v)
    return df


def _export_csv(gdf, name, target_crs, tmp_dir) -> bytes:
    csv_gdf = gdf.copy()
    # Label lat-lon for geographic CRS and easting-northing for projected.
    if target_crs.is_geographic:
        csv_gdf['longitude'] = csv_gdf.geometry.x
        csv_gdf['latitude'] = csv_gdf.geometry.y
    else:
        csv_gdf['easting'] = csv_gdf.geometry.x
        csv_gdf['northing'] = csv_gdf.geometry.y
    csv_gdf = csv_safe_frame(csv_gdf.drop(columns='geometry'))
    buf = io.StringIO()
    csv_gdf.to_csv(buf, index=False)
    return buf.getvalue().encode()


def _export_filegdb(gdf, name, target_crs, tmp_dir) -> bytes:
    # File geodatabases are directories and must be zipped for download.
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
        return fh.read()


def _export_geojson(gdf, name, target_crs, tmp_dir) -> bytes:
    out_path = os.path.join(tmp_dir, f'{name}.geojson')
    gdf.to_file(out_path, driver='GeoJSON')
    with open(out_path, 'rb') as fh:
        return fh.read()


def _export_geopackage(gdf, name, target_crs, tmp_dir) -> bytes:
    out_path = os.path.join(tmp_dir, f'{name}.gpkg')
    gdf.to_file(out_path, driver='GPKG', layer=name)
    with open(out_path, 'rb') as fh:
        return fh.read()


def _export_kml(gdf, name, target_crs, tmp_dir) -> bytes:
    # KML requires WGS 84; LIBKML preserves the GeoDataFrame's field layout.
    kml_gdf = gdf.to_crs('EPSG:4326')
    kml_gdf = kml_gdf.copy()
    kml_gdf['Name'] = kml_gdf['filename']
    out_path = os.path.join(tmp_dir, f'{name}.kml')
    kml_gdf.to_file(out_path, driver='LIBKML')
    with open(out_path, 'rb') as fh:
        return fh.read()


def _export_shapefile(gdf, name, target_crs, tmp_dir) -> bytes:
    # A shapefile download must include its supporting files.
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
        return fh.read()


_EXPORT_HANDLERS = {
    'csv': _export_csv,
    'filegdb': _export_filegdb,
    'geojson': _export_geojson,
    'geopackage': _export_geopackage,
    'kml': _export_kml,
    'shapefile': _export_shapefile,
}


def build_standard_export(fmt: str, gdf, name: str, target_crs, tmp_dir: str) -> bytes:
    return _EXPORT_HANDLERS[fmt](gdf, name, target_crs, tmp_dir)
