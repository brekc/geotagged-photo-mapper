import io
import os
import re
import urllib.request
import warnings
warnings.filterwarnings("ignore", message="pyproj unable to set PROJ database path")
import json
import tempfile
import zipfile
import shutil
from typing import List

import pandas as pd

import geopandas as gpd
from fastapi import FastAPI, UploadFile, File, Form, Query, Request, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from exiftool import ExifToolHelper
from shapely.geometry import Point
from pyproj import CRS
from pyproj.database import query_crs_info
from pyproj.enums import PJType

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

cached_features: list = []

# Load all EPSG projected CRS once at startup for fast in-process filtering
try:
    _ALL_PROJECTED_CRS = list(query_crs_info(
        auth_name='EPSG',
        pj_types=PJType.PROJECTED_CRS,
        allow_deprecated=False,
    ))
except Exception:
    _ALL_PROJECTED_CRS = []

_DATA_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
_SP_CSV_URL   = 'https://raw.githubusercontent.com/ret3/stateplane/master/state_plane_reference.csv'
_COUNTIES_URL = 'https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_20m.zip'
_sp_zones_cache: dict | None = None


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

    merged = counties_gdf.merge(sp_df, on='fips', how='inner')
    zones_gdf = merged.dissolve(by='nad83_epsg').reset_index()
    zones_gdf = zones_gdf.to_crs('EPSG:4326')

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
    # Canadian provinces / territories
    'AB': 'Alberta', 'BC': 'British Columbia', 'MB': 'Manitoba', 'NB': 'New Brunswick',
    'NL': 'Newfoundland', 'NS': 'Nova Scotia', 'NT': 'Northwest Territories',
    'NU': 'Nunavut', 'ON': 'Ontario', 'PE': 'Prince Edward Island',
    'QC': 'Quebec', 'SK': 'Saskatchewan', 'YT': 'Yukon',
}


# ---------------------------------------------------------------------------
# GPS extraction
# ---------------------------------------------------------------------------

def extract_gps(file_paths):
    features = []

    with ExifToolHelper() as et:
        metadata_list = et.get_metadata(file_paths)

    for meta in metadata_list:
        # Try Composite tags first, fall back to EXIF tags
        lat = meta.get('Composite:GPSLatitude') or meta.get('EXIF:GPSLatitude')
        lon = meta.get('Composite:GPSLongitude') or meta.get('EXIF:GPSLongitude')

        if lat is None or lon is None:
            continue

        lat = float(lat)
        lon = float(lon)

        # Composite tags already carry sign; only apply ref when using raw EXIF tags
        if not meta.get('Composite:GPSLatitude'):
            if meta.get('EXIF:GPSLatitudeRef', '').upper() == 'S':
                lat = -abs(lat)
            if meta.get('EXIF:GPSLongitudeRef', '').upper() == 'W':
                lon = -abs(lon)

        alt_raw = meta.get('Composite:GPSAltitude') or meta.get('EXIF:GPSAltitude')
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

def build_geojson(features):
    geometries = [Point(f['longitude'], f['latitude']) for f in features]
    properties = [
        {k: v for k, v in f.items() if k not in ('latitude', 'longitude')}
        for f in features
    ]
    gdf = gpd.GeoDataFrame(properties, geometry=geometries, crs='EPSG:4326')
    return gdf.to_json()


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
def zone_geojson(zone_type: str = Query(..., alias='type')):
    if zone_type != 'state_plane':
        raise HTTPException(status_code=400, detail='type must be state_plane')
    return _get_sp_zones()


@app.get('/crs-search')
async def crs_search(q: str = Query(default='')):
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail='Query must be at least 2 characters')

    # Expand state / province abbreviation to full name (e.g. "WA" -> "Washington")
    term = STATE_ABBR.get(q.upper(), q)

    escaped = re.escape(term)
    # Match "- {term}" at state level — negative lookahead excludes county/parish/borough
    # suffixes so e.g. "WisCRS Washington County" doesn't appear for a Washington state search
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
async def export(
    format: str = Form(...),
    epsg: str = Form(...),
    source_path: str = Form(default=''),
    flight_altitude: str = Form(default=''),
    altitude_unit: str = Form(default='feet'),
):
    fmt = format.lower()

    try:
        epsg_int = int(epsg)
        target_crs = CRS.from_epsg(epsg_int)
    except Exception:
        raise HTTPException(status_code=400, detail=f'Invalid EPSG code: {epsg}')

    global cached_features
    if not cached_features:
        raise HTTPException(status_code=400, detail='No data to export — upload photos first')

    geometries = [Point(f['longitude'], f['latitude']) for f in cached_features]
    properties = [
        {k: v for k, v in f.items() if k not in ('latitude', 'longitude')}
        for f in cached_features
    ]
    gdf = gpd.GeoDataFrame(properties, geometry=geometries, crs='EPSG:4326')

    # Source path: only add column when a path was provided
    clean_source = source_path.strip()
    if clean_source:
        if not clean_source.endswith(('/', '\\')):
            sep = '\\' if '\\' in clean_source else '/'
            clean_source += sep
        gdf['source'] = gdf['filename'].apply(lambda fn: clean_source + fn)

    # Flight altitude: only add columns when a value was provided
    try:
        alt_val = float(flight_altitude) if flight_altitude.strip() else None
    except ValueError:
        alt_val = None

    if alt_val is not None:
        if altitude_unit == 'meters':
            gdf['flight_alt_m']  = round(alt_val, 1)
            gdf['flight_alt_ft'] = round(alt_val / 0.3048, 1)
        else:
            gdf['flight_alt_ft'] = round(alt_val, 1)
            gdf['flight_alt_m']  = round(alt_val * 0.3048, 1)

    gdf = gdf.to_crs(target_crs)

    tmp_dir = tempfile.mkdtemp()
    try:
        if fmt == 'geojson':
            out_path = os.path.join(tmp_dir, 'photo_locations.geojson')
            gdf.to_file(out_path, driver='GeoJSON')
            with open(out_path, 'rb') as fh:
                content = fh.read()
            return Response(
                content=content,
                media_type='application/geo+json',
                headers={'Content-Disposition': 'attachment; filename="photo_locations.geojson"'},
            )

        elif fmt == 'geopackage':
            out_path = os.path.join(tmp_dir, 'photo_locations.gpkg')
            gdf.to_file(out_path, driver='GPKG', layer='photo_locations')
            with open(out_path, 'rb') as fh:
                content = fh.read()
            return Response(
                content=content,
                media_type='application/geopackage+sqlite3',
                headers={'Content-Disposition': 'attachment; filename="photo_locations.gpkg"'},
            )

        elif fmt == 'filegdb':
            gdb_path = os.path.join(tmp_dir, 'photo_locations.gdb')
            gdf.to_file(gdb_path, driver='OpenFileGDB', layer='photo_locations')
            zip_path = os.path.join(tmp_dir, 'photo_locations_gdb.zip')
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
                headers={'Content-Disposition': 'attachment; filename="photo_locations_gdb.zip"'},
            )

        elif fmt == 'shapefile':
            shp_dir = os.path.join(tmp_dir, 'shapefile')
            os.makedirs(shp_dir)
            shp_path = os.path.join(shp_dir, 'photo_locations.shp')
            gdf.to_file(shp_path, driver='ESRI Shapefile')
            zip_path = os.path.join(tmp_dir, 'photo_locations_shp.zip')
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for ext in ('.shp', '.shx', '.dbf', '.prj', '.cpg'):
                    candidate = os.path.join(shp_dir, f'photo_locations{ext}')
                    if os.path.exists(candidate):
                        zf.write(candidate, f'photo_locations{ext}')
            with open(zip_path, 'rb') as fh:
                content = fh.read()
            return Response(
                content=content,
                media_type='application/zip',
                headers={'Content-Disposition': 'attachment; filename="photo_locations_shp.zip"'},
            )

        elif fmt == 'kml':
            # KML spec requires WGS 84
            kml_gdf = gdf.to_crs('EPSG:4326')
            out_path = os.path.join(tmp_dir, 'photo_locations.kml')
            kml_gdf.to_file(out_path, driver='KML')
            with open(out_path, 'rb') as fh:
                content = fh.read()
            return Response(
                content=content,
                media_type='application/vnd.google-earth.kml+xml',
                headers={'Content-Disposition': 'attachment; filename="photo_locations.kml"'},
            )

        elif fmt == 'csv':
            csv_gdf = gdf.copy()
            if target_crs.is_geographic:
                csv_gdf['longitude'] = csv_gdf.geometry.x
                csv_gdf['latitude']  = csv_gdf.geometry.y
            else:
                csv_gdf['easting']  = csv_gdf.geometry.x
                csv_gdf['northing'] = csv_gdf.geometry.y
            csv_gdf = csv_gdf.drop(columns='geometry')
            buf = io.StringIO()
            csv_gdf.to_csv(buf, index=False)
            return Response(
                content=buf.getvalue().encode(),
                media_type='text/csv',
                headers={'Content-Disposition': 'attachment; filename="photo_locations.csv"'},
            )

        else:
            raise HTTPException(status_code=400, detail=f'Unknown format: {fmt}')

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('geotagged_photo_mapper:app', host='0.0.0.0', port=8000)
