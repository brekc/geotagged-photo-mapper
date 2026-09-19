"""Regression protection for the PROJ initialization block.

This asserts the exact protected snippet from geotagged_photo_mapper.py is
byte-for-byte unchanged, plus a few functional smoke checks that the CRS
machinery it enables (custom CRS parsing, CRS search, State Plane/UTM
support) still works. If this test's PROTECTED_BLOCK constant needs to
change, that is itself a sign the invariant was touched -- update the
constant only alongside an explicit, deliberate decision to change the PROJ
setup itself.
"""

import inspect
import os

import geotagged_photo_mapper as app_module

PROTECTED_BLOCK = '''# Set PROJ grid cache before importing. This will preserve the grid cache
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
set_network_enabled(True)'''


def test_proj_block_is_byte_for_byte_unchanged():
    source_path = inspect.getsourcefile(app_module)
    with open(source_path, encoding='utf-8') as f:
        source = f.read()
    assert PROTECTED_BLOCK in source, (
        'The protected PROJ initialization block in geotagged_photo_mapper.py '
        'has changed. This block must be preserved byte-for-byte.'
    )


def test_data_dir_is_a_plain_string_not_a_path_object():
    # Explicitly required: _DATA_DIR must not be converted to pathlib.Path.
    assert isinstance(app_module._DATA_DIR, str)
    assert app_module._DATA_DIR == os.path.join(
        os.path.dirname(os.path.abspath(app_module.__file__)), 'data',
    )


def test_proj_lib_and_proj_data_are_cleared():
    assert os.environ.get('PROJ_LIB') is None
    assert os.environ.get('PROJ_DATA') is None


def test_custom_crs_parsing_still_works():
    crs = app_module._parse_custom_crs('EPSG:4326')
    assert crs.to_epsg() == 4326


def test_custom_crs_parsing_rejects_garbage():
    import pytest
    with pytest.raises(ValueError):
        app_module._parse_custom_crs('not a real CRS definition')


def test_state_plane_zone_endpoint_reuses_existing_path(client):
    res = client.get('/zone-geojson?type=state_plane')
    assert res.status_code == 200
    body = res.json()
    assert body.get('type') == 'FeatureCollection'


def test_crs_search_reuses_existing_path(client):
    res = client.get('/crs-search?q=Oregon')
    assert res.status_code == 200
    results = res.json()
    assert isinstance(results, list)
    assert any('Oregon' in r['area'] for r in results)


def test_standard_export_reprojection_pathway(client, make_jpeg):
    path = make_jpeg(lat=45.5231, lon=-122.6765)
    with open(path, 'rb') as f:
        res = client.post('/upload', files={'photos': ('photo.jpg', f, 'image/jpeg')})
    assert res.status_code == 200
    body = res.json()
    upload_id = body['upload_id']
    assert body['total_geotagged'] == 1

    res = client.post('/export', data={
        'format': 'csv',
        'upload_id': upload_id,
        'epsg': '32610',  # WGS 84 / UTM zone 10N -- exercises real reprojection
    })
    assert res.status_code == 200
    text = res.content.decode()
    assert 'easting' in text and 'northing' in text
