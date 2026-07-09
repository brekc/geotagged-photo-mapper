"""Geotagged Photo Mapper backend.

A small FastAPI app with four jobs:
  1. Accept uploaded photos and pull GPS coordinates out of their EXIF data.
  2. Serve those points back to the browser as GeoJSON so Leaflet can plot them.
  3. Let the user search for a coordinate reference system (CRS) to export into,
     either from a curated list, a region search, a manual EPSG code, or a
     pasted/uploaded custom definition (WKT or PROJ4).
  4. Reproject the cached points into that CRS and stream them back as a file
     in one of several GIS formats.

Nothing is written to disk except the temporary files needed to build each
export, and the State Plane zone cache described below.
"""

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

# The most recent /upload result, held in memory so /export can reproject and
# reformat it without asking the browser to re-send the photos. This means
# the app only supports one "session" at a time (a second upload replaces the
# first), which is fine for a local single-user tool but wouldn't be safe for
# multiple concurrent users.
cached_features: list = []

# Load every projected EPSG CRS once at startup so /crs-search can filter an
# in-memory list on each request instead of hitting the PROJ database every
# time. This list is what the "Region" dropdown searches through.
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
    """Build the US State Plane zone polygons and cache them to disk.

    There's no single official "State Plane zones" shapefile, so this stitches
    one together: a state-plane reference CSV (which zone each county belongs
    to) is joined against the Census Bureau's county boundary shapefile, then
    counties in the same zone are dissolved into one polygon per zone. Both
    source files are downloaded once and cached under data/, and the CSV
    join only keeps NAD83 zones since that's the current, non-deprecated
    datum (see the "State Plane: NAD83 only" note in the README).
    """
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

    # The county shapefile only needs its FIPS code (used to join against the
    # state-plane CSV below) and its geometry.
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

    # Join counties to their zone, then dissolve (merge geometries) so each
    # zone becomes a single polygon instead of one polygon per county.
    merged = counties_gdf.merge(sp_df, on='fips', how='inner')
    zones_gdf = merged.dissolve(by='nad83_epsg').reset_index()
    zones_gdf = zones_gdf.to_crs('EPSG:4326')

    def _crs_info(epsg: int):
        # Look up the human-readable name and area-of-use for each zone's
        # EPSG code, for the map popup and export label.
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
    """Return the State Plane zone GeoJSON, building and caching it on first use.

    Three layers of caching here, cheapest first: an in-memory dict for the
    life of the process, then a file on disk (data/state_plane_zones.geojson)
    that survives restarts, and only if neither exists do we pay the cost of
    downloading and dissolving the source data in _build_sp_zones().
    """
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


# Lets a user type a two-letter state/province code (e.g. "WA") into the
# region search box and have it expand to the full name that the CRS
# area-of-use strings actually use ("Washington").
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

def _coalesce(*values):
    """Return the first value that isn't None.

    Used instead of `a or b` for the Composite/EXIF GPS tag fallback below,
    since `or` treats a legitimate 0.0 (equator, prime meridian, sea level)
    as falsy and would skip straight to the fallback value.
    """
    for v in values:
        if v is not None:
            return v
    return None


def extract_gps(file_paths):
    """Read GPS and camera metadata out of a batch of photo files via ExifTool.

    Returns a list of plain dicts (one per photo that actually has GPS data),
    which is the shape the rest of the app works with all the way through to
    export, so no photo file needs to be touched again after this point.
    """
    features = []

    with ExifToolHelper() as et:
        metadata_list = et.get_metadata(file_paths)

    for meta in metadata_list:
        # ExifTool's "Composite" tags are its own best-effort combination of
        # the raw EXIF GPS tags (already converted to signed decimal degrees),
        # so prefer them and only fall back to raw EXIF tags if a photo's
        # metadata doesn't have a Composite tag for some reason. This uses
        # explicit `is not None` checks rather than `x or y`, since a photo
        # taken exactly on the equator, the prime meridian, or at sea level
        # has a legitimate value of 0.0, which `or` would treat as falsy and
        # incorrectly skip in favor of the fallback.
        composite_lat = meta.get('Composite:GPSLatitude')
        composite_lon = meta.get('Composite:GPSLongitude')
        lat = _coalesce(composite_lat, meta.get('EXIF:GPSLatitude'))
        lon = _coalesce(composite_lon, meta.get('EXIF:GPSLongitude'))

        if lat is None or lon is None:
            continue

        lat = float(lat)
        lon = float(lon)

        # Composite tags already carry the correct sign (negative = south/west).
        # Raw EXIF tags store an unsigned value plus a separate "Ref" tag
        # (e.g. GPSLatitudeRef = 'S'). Latitude and longitude are computed as
        # separate Composite tags by ExifTool, so each needs its own check
        # here: it's possible for one to be present and the other to have
        # fallen back to the raw tag.
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

def build_geojson(features):
    """Turn a list of extract_gps() dicts into a GeoJSON FeatureCollection string.

    latitude/longitude become the point geometry; everything else in each
    dict becomes a GeoJSON "properties" field (filename, altitude, datetime,
    camera_model), which the frontend reads to build map popups.
    """
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

def _parse_custom_crs(text: str) -> CRS:
    """Parse a pasted or uploaded custom CRS definition.

    This backs the "Custom CRS" export field, which accepts either text
    pasted directly into the textarea or the contents of an uploaded .prj
    file (the frontend just reads the file as text and reuses the same
    field). CRS.from_user_input() already understands WKT, PROJ4, PROJJSON,
    and authority strings like "ESRI:102008", so it covers most real-world
    inputs on its own. The CRS.from_wkt() fallback exists for the odd
    ESRI-flavored .prj file that from_user_input() can't classify by itself.
    """
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
    """Receive photo uploads, extract GPS data, and cache it for /export.

    Files are written to a temp directory only because ExifTool needs real
    file paths to read from (it can't read from in-memory bytes), and that
    temp directory is deleted again in the `finally` block regardless of
    whether extraction succeeded.
    """
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

        # Replaces whatever was uploaded previously; see the comment on
        # cached_features above for why there's only ever one "session".
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
    """Return reference-layer zone polygons for the map.

    Only serves State Plane zones. UTM zones don't need a backend route at
    all, since they're simple 6-degree-wide rectangles that the frontend
    generates by formula (see buildUtmLayer() in the JS).
    """
    if zone_type != 'state_plane':
        raise HTTPException(status_code=400, detail='type must be state_plane')
    return _get_sp_zones()


@app.get('/crs-search')
async def crs_search(q: str = Query(default='')):
    """Search the projected-CRS list by region name, for the "Region" dropdown.

    Matching is done against each CRS's "area of use" string (e.g.
    "United States (USA) - Washington"), not its name, since that's where
    the state/country name actually lives.
    """
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail='Query must be at least 2 characters')

    # Expand a state / province abbreviation to its full name (e.g. "WA" -> "Washington")
    # so users can type either.
    term = STATE_ABBR.get(q.upper(), q)

    escaped = re.escape(term)
    # Match "- {term}" at state level. The negative lookahead excludes
    # county/parish/borough-level area-of-use strings, so e.g. a Washington
    # state search doesn't also return every "Washington County" CRS.
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
    epsg: str = Form(default=''),
    custom_crs: str = Form(default=''),
    source_path: str = Form(default=''),
    flight_altitude: str = Form(default=''),
    altitude_unit: str = Form(default='feet'),
):
    """Reproject the cached photo points and stream them back as a file.

    The target CRS comes from one of two places: a pasted/uploaded custom
    CRS definition (custom_crs) if provided, otherwise a plain EPSG code
    (epsg). custom_crs takes priority, matching how the frontend already
    lets the manual EPSG field override the region/common CRS pickers.
    """
    fmt = format.lower()

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

    global cached_features
    if not cached_features:
        raise HTTPException(status_code=400, detail='No data to export, upload photos first')

    geometries = [Point(f['longitude'], f['latitude']) for f in cached_features]
    properties = [
        {k: v for k, v in f.items() if k not in ('latitude', 'longitude')}
        for f in cached_features
    ]
    gdf = gpd.GeoDataFrame(properties, geometry=geometries, crs='EPSG:4326')

    # Source path: only add the column when a path was actually provided, so
    # exports where the user left it blank don't get an empty column.
    clean_source = source_path.strip()
    if clean_source:
        if not clean_source.endswith(('/', '\\')):
            sep = '\\' if '\\' in clean_source else '/'
            clean_source += sep
        gdf['source'] = gdf['filename'].apply(lambda fn: clean_source + fn)

    # Flight altitude: same "only add columns if provided" rule as source
    # path above. flight_altitude arrives as a plain string (not a FastAPI
    # Optional[float] form field) to sidestep FastAPI's coercion of an empty
    # string into a validation error rather than None.
    try:
        alt_val = float(flight_altitude) if flight_altitude.strip() else None
    except ValueError:
        alt_val = None

    if alt_val is not None:
        # Always store both units regardless of which one was entered, so
        # downstream consumers of the export don't need to know which unit
        # the user originally typed in.
        if altitude_unit == 'meters':
            gdf['flight_alt_m']  = round(alt_val, 1)
            gdf['flight_alt_ft'] = round(alt_val / 0.3048, 1)
        else:
            gdf['flight_alt_ft'] = round(alt_val, 1)
            gdf['flight_alt_m']  = round(alt_val * 0.3048, 1)

    gdf = gdf.to_crs(target_crs)

    tmp_dir = tempfile.mkdtemp()
    try:
        # Format handlers below are ordered alphabetically by format name to
        # make a given format quick to find; they're independent of each
        # other, so the order has no effect on behavior.
        if fmt == 'csv':
            csv_gdf = gdf.copy()
            # Geographic CRS (like WGS 84) uses lon/lat; projected CRS (like
            # a State Plane or UTM zone) uses easting/northing, since "lat/lon"
            # would be a misleading label for coordinates in meters or feet.
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

        elif fmt == 'filegdb':
            # A File Geodatabase is itself a directory of files, so it has to
            # be zipped up before it can be sent as a single HTTP response.
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

        elif fmt == 'geojson':
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

        elif fmt == 'kml':
            # The KML spec requires coordinates in WGS 84, so reproject to it
            # regardless of what CRS the user picked for other formats.
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

        elif fmt == 'shapefile':
            # A shapefile is really a set of sibling files (.shp/.shx/.dbf/
            # .prj/.cpg) that all have to travel together, so like FileGDB
            # above, it gets zipped before being returned.
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

        else:
            raise HTTPException(status_code=400, detail=f'Unknown format: {fmt}')

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('geotagged_photo_mapper:app', host='0.0.0.0', port=8000)
