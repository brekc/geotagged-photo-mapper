"""geotagged_photo_mapper.py

The backend implementing FastAPI with four tasks:
  1. Import uploaded photos and pull GPS coordinates from EXIF data.
  2. Return GPS coordinates to the browser in GeoJSON format for plotting with Leaflet.
  3. Let the user search for a coordinate reference system (CRS) from a
     curated list, a region search, a manual EPSG code, or a
     pasted/uploaded custom definition (WKT or PROJ4).
  4. Reproject the cached points into the target CRS and stream them back as a file
     in one of several GIS formats.

Nothing is written to disk except the temporary files needed to build each
export and the State Plane zone cache described below.
"""

import io
import json
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from typing import List

import pandas as pd
from exiftool import ExifToolHelper
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from shapely.geometry import Point

# Set PROJ grid cache before importing. This will preserve the grid cache
# following container restarts.
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
os.environ.setdefault('PROJ_USER_WRITABLE_DIRECTORY', os.path.join(_DATA_DIR, 'proj_cache'))

# PROJ_LIB and PROJ_DATA can point to an incompatible system copy from a
# PostgreSQL/PostGIS installation. Clear these to match the environment. 
os.environ.pop('PROJ_LIB', None)
os.environ.pop('PROJ_DATA', None)

import geopandas as gpd
from pyproj import CRS
from pyproj.database import query_crs_info
from pyproj.enums import PJType
from pyproj.network import set_network_enabled

# Allow PROJ to download and cache the latest shift-grid files for
# datum transformations (e.g. NAD83(HARN) -> NAD83(2011))
set_network_enabled(True)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Cached /upload result for /export. This will handle one upload at a time,
# meaning that subsequent uploads will replace what is in memory. 
cached_features: list = []

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

# Build US State Plane zones by joining a state plane reference CSV and the
# Census Bureau's county boundaries. Dissolving by zone and caching the result
# will keep the NAD83 zones as a reference layer.
def _build_sp_zones(cache_path: str) -> dict:
    os.makedirs(_DATA_DIR, exist_ok=True)

    csv_path = os.path.join(_DATA_DIR, 'state_plane_reference.csv')
    if not os.path.exists(csv_path):
        urllib.request.urlretrieve(_SP_CSV_URL, csv_path)

    counties_dir = os.path.join(_DATA_DIR, 'counties_20m')
    if not os.path.exists(counties_dir):
        zip_path = os.path.join(_DATA_DIR, 'cb_2023_us_county_20m.zip')
        urllib.request.urlretrieve(_COUNTIES_URL, zip_path)
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
    zones_gdf = zones_gdf.to_crs('EPSG:4326')

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
    with open(cache_path, 'w') as f:
        json.dump(result, f)
    return result

# Return State Plane zone GeoJSON. Three cached layers will include
# in-memory dict, on-disk file (data/state_plane_zones.geojson), and
# a full build of _build_sp_zones() if neither exists.
def _get_sp_zones() -> dict:
    global _sp_zones_cache
    if _sp_zones_cache is not None:
        return _sp_zones_cache
    cache_path = os.path.join(_DATA_DIR, 'state_plane_zones.geojson')
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            _sp_zones_cache = json.load(f)
        return _sp_zones_cache
    _sp_zones_cache = _build_sp_zones(cache_path)
    return _sp_zones_cache

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
# GPS extraction
# ---------------------------------------------------------------------------

# Return the first non-None value. Safer than `or` since 0.0 is valid.
def _coalesce(*values):
    for v in values:
        if v is not None:
            return v
    return None

# Extract GPS and camera metadata from photos via ExifTool. This will
# return a list of dicts (one per geotagged photo).
def extract_gps(file_paths):
    features = []
    with ExifToolHelper() as et:
        metadata_list = et.get_metadata(file_paths)
    for meta in metadata_list:
        # Prefer Composite tags (signed decimal degrees); fall back to raw EXIF.
        composite_lat = meta.get('Composite:GPSLatitude')
        composite_lon = meta.get('Composite:GPSLongitude')
        lat = _coalesce(composite_lat, meta.get('EXIF:GPSLatitude'))
        lon = _coalesce(composite_lon, meta.get('EXIF:GPSLongitude'))

        if lat is None or lon is None:
            continue
          
        lat = float(lat)
        lon = float(lon)
        
        # Raw EXIF tags are unsigned; apply Ref tag sign if used.
        if composite_lat is None and meta.get('EXIF:GPSLatitudeRef', '').upper() == 'S':
            lat = -abs(lat)
        if composite_lon is None and meta.get('EXIF:GPSLongitudeRef', '').upper() == 'W':
            lon = -abs(lon)

        alt_raw = _coalesce(meta.get('Composite:GPSAltitude'), meta.get('EXIF:GPSAltitude'))
        altitude_m = float(alt_raw) if alt_raw is not None else None
        altitude_ft = round(altitude_m * 3.28084, 1) if altitude_m is not None else None

        filename = os.path.basename(meta.get('SourceFile', ''))

        features.append({
            'filename': filename,
            'latitude': lat,
            'longitude': lon,
            'altitude_m': altitude_m,
            'altitude_ft': altitude_ft,
            'datetime': meta.get('EXIF:DateTimeOriginal'),
            'camera_model': meta.get('EXIF:Model'),
        })

    return features

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

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get('/')
async def index(request: Request):
    return templates.TemplateResponse(request, 'geotagged-photo-mapper.html')

@app.post('/upload')
async def upload(
    photos: List[UploadFile] = File(...),
):
    # Extract GPS from uploaded photos and cache results for /export.
    # Temp files are needed because ExifTool requires real file paths.
    if not photos:
        raise HTTPException(status_code=400, detail='No files received')

    tmp_dir = tempfile.mkdtemp()
    try:
        saved_paths = []
        for f in photos:
            dest = os.path.join(tmp_dir, f.filename)
            content = await f.read()
            with open(dest, 'wb') as out:
                out.write(content)
            saved_paths.append(dest)

        features = extract_gps(saved_paths)
        geojson = build_geojson(features) if features else json.dumps({
            'type': 'FeatureCollection', 'features': []
        })

        # Replace previous upload
        global cached_features
        cached_features = features

        return {
            'geojson': json.loads(geojson),
            'total_uploaded': len(saved_paths),
            'total_geotagged': len(features),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

@app.get('/zone-geojson')
# Return State Plane zone polygons. UTM zones are generated through the frontend 
# with buildUtmLayer().
def zone_geojson(zone_type: str = Query(..., alias='type')):
    if zone_type != 'state_plane':
        raise HTTPException(status_code=400, detail='type must be state_plane')
    return _get_sp_zones()

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
# Reproject cached photo points and return as a downloadable file.
# custom_crs will take priority over epsg.
async def export(
    format: str = Form(...),
    epsg: str = Form(default=''),
    custom_crs: str = Form(default=''),
    source_path: str = Form(default=''),
    flight_altitude: str = Form(default=''),
    altitude_unit: str = Form(default='feet'),
    export_name: str = Form(default='photo_locations'),
):
  
    fmt = format.lower()

    # Sanatize for an internal layer name.
    name = re.sub(r'[\\/:*?"<>|]', '_', export_name.strip()) or 'photo_locations'

    custom_crs = custom_crs.strip()
    if custom_crs:
        try:
            target_crs = _parse_custom_crs(custom_crs)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif epsg.strip():
        try:
            epsg_int = int(epsg)
            target_crs = CRS.from_epsg(epsg_int)
        except Exception:
            raise HTTPException(status_code=400, detail=f'Invalid EPSG code: {epsg}')
    else:
        raise HTTPException(status_code=400, detail='No EPSG code or custom CRS provided')

    if not cached_features:
        raise HTTPException(status_code=400, detail='No data to export, upload photos first')

    geometries = [Point(f['longitude'], f['latitude']) for f in cached_features]
    properties = [
        {k: v for k, v in f.items() if k not in ('latitude', 'longitude')}
        for f in cached_features
    ]
    gdf = gpd.GeoDataFrame(properties, geometry=geometries, crs='EPSG:4326')

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
            csv_gdf = csv_gdf.drop(columns='geometry')
            buf = io.StringIO()
            csv_gdf.to_csv(buf, index=False)
            return Response(
                content=buf.getvalue().encode(),
                media_type='text/csv',
                headers={'Content-Disposition': f'attachment; filename="{name}.csv"'},
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
                headers={'Content-Disposition': f'attachment; filename="{name}_gdb.zip"'},
            )

        elif fmt == 'geojson':
            out_path = os.path.join(tmp_dir, f'{name}.geojson')
            gdf.to_file(out_path, driver='GeoJSON')
            with open(out_path, 'rb') as fh:
                content = fh.read()
            return Response(
                content=content,
                media_type='application/geo+json',
                headers={'Content-Disposition': f'attachment; filename="{name}.geojson"'},
            )

        elif fmt == 'geopackage':
            out_path = os.path.join(tmp_dir, f'{name}.gpkg')
            gdf.to_file(out_path, driver='GPKG', layer=name)
            with open(out_path, 'rb') as fh:
                content = fh.read()
            return Response(
                content=content,
                media_type='application/geopackage+sqlite3',
                headers={'Content-Disposition': f'attachment; filename="{name}.gpkg"'},
            )

        elif fmt == 'kml':
            # KML requires WGS 84 coordinates.
            kml_gdf = gdf.to_crs('EPSG:4326')
            out_path = os.path.join(tmp_dir, f'{name}.kml')
            kml_gdf.to_file(out_path, driver='KML')
            with open(out_path, 'rb') as fh:
                content = fh.read()
            return Response(
                content=content,
                media_type='application/vnd.google-earth.kml+xml',
                headers={'Content-Disposition': f'attachment; filename="{name}.kml"'},
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
                headers={'Content-Disposition': f'attachment; filename="{name}_shp.zip"'},
            )

        else:
            raise HTTPException(status_code=400, detail=f'Unknown format: {fmt}')

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('geotagged_photo_mapper:app', host='0.0.0.0', port=8000)
