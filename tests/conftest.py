"""Shared fixtures for the test suite.

Every fixture generates synthetic images in a pytest tmp_path -- no personal
photos, real GPS locations, or other real-world data is ever used or
committed. GPS tags are written with ExifTool onto plain solid-color
placeholder images.
"""

import shutil

import pytest
from exiftool import ExifToolHelper
from PIL import Image

EXIFTOOL_AVAILABLE = shutil.which('exiftool') is not None


def _write_tags(path: str, tags: dict) -> None:
    with ExifToolHelper() as et:
        et.set_tags([path], tags=tags, params=['-overwrite_original'])


def _gps_tags(lat, lon, alt=None, alt_below_sea_level=False, heading=None, heading_ref='T'):
    tags = {}
    if lat is not None:
        tags['GPSLatitude'] = abs(lat)
        tags['GPSLatitudeRef'] = 'N' if lat >= 0 else 'S'
    if lon is not None:
        tags['GPSLongitude'] = abs(lon)
        tags['GPSLongitudeRef'] = 'E' if lon >= 0 else 'W'
    if alt is not None:
        tags['GPSAltitude'] = abs(alt)
        tags['GPSAltitudeRef'] = 1 if alt_below_sea_level else 0
    if heading is not None:
        tags['GPSImgDirection'] = heading
        tags['GPSImgDirectionRef'] = heading_ref
    return tags


@pytest.fixture(autouse=True)
def _require_exiftool():
    if not EXIFTOOL_AVAILABLE:
        pytest.skip('exiftool is not installed on PATH')


@pytest.fixture
def make_jpeg(tmp_path):
    def _make(name='photo.jpg', lat=45.5, lon=-122.6, size=(80, 60), extra_tags=None, **gps_kwargs):
        path = tmp_path / name
        Image.new('RGB', size, (120, 160, 200)).save(str(path), format='JPEG')
        tags = _gps_tags(lat, lon, **gps_kwargs)
        if extra_tags:
            tags.update(extra_tags)
        if tags:
            _write_tags(str(path), tags)
        return str(path)
    return _make


@pytest.fixture
def make_png(tmp_path):
    def _make(name='photo.png', lat=45.5, lon=-122.6, size=(80, 60), extra_tags=None, **gps_kwargs):
        path = tmp_path / name
        Image.new('RGB', size, (200, 160, 120)).save(str(path), format='PNG')
        tags = _gps_tags(lat, lon, **gps_kwargs)
        if extra_tags:
            tags.update(extra_tags)
        if tags:
            _write_tags(str(path), tags)
        return str(path)
    return _make


@pytest.fixture
def make_heic(tmp_path):
    """Encodes a real HEIC file via pillow-heif. Skips the test if this
    environment's libheif build has no HEIC encoder (encoding needs an x265
    plugin that isn't guaranteed to ship with every libheif install)."""
    def _make(name='photo.heic', lat=45.5, lon=-122.6, size=(80, 60), extra_tags=None, **gps_kwargs):
        import pillow_heif
        pillow_heif.register_heif_opener()
        path = tmp_path / name
        try:
            Image.new('RGB', size, (160, 200, 120)).save(str(path), format='HEIF', quality=80)
        except Exception as e:
            pytest.skip(f'HEIC encoding not available in this environment: {e}')
        tags = _gps_tags(lat, lon, **gps_kwargs)
        if extra_tags:
            tags.update(extra_tags)
        if tags:
            _write_tags(str(path), tags)
        return str(path)
    return _make


@pytest.fixture
def corrupt_jpeg(tmp_path):
    path = tmp_path / 'corrupt.jpg'
    path.write_bytes(b'\xff\xd8\xff\xe0not a real jpeg body')
    return str(path)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    import geotagged_photo_mapper as app_module
    return TestClient(app_module.app)
