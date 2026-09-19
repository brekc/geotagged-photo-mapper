"""Tests for HEIC/HEIF support and GPS/camera metadata extraction
(geotagged_photo_mapper.py)."""

import pytest

import geotagged_photo_mapper as app_module


# ---------------------------------------------------------------------------
# Extension / MIME validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('filename,content_type', [
    ('a.jpg', 'image/jpeg'),
    ('A.JPG', 'image/jpeg'),
    ('a.jpeg', 'image/jpeg'),
    ('a.png', 'image/png'),
    ('a.heic', 'image/heic'),
    ('a.HEIC', 'image/heic'),
    ('a.heif', 'image/heif'),
    ('a.heic', 'image/heic-sequence'),
    ('a.heif', 'image/heif-sequence'),
    ('a.heic', ''),  # many browsers/OSes send no Content-Type for HEIC
    ('a.heic', 'application/octet-stream'),
    ('a.jpg', None),
])
def test_validate_upload_extension_accepts_supported_variants(filename, content_type):
    ext = app_module._validate_upload_extension(filename, content_type)
    assert ext in app_module.ALLOWED_EXTENSIONS


@pytest.mark.parametrize('filename,content_type', [
    ('a.gif', 'image/gif'),
    ('a.bmp', 'image/bmp'),
    ('a.txt', 'text/plain'),
    ('a', ''),
    ('a.jpg', 'text/html'),
    ('a.jpg', 'application/x-msdownload'),
])
def test_validate_upload_extension_rejects_unsupported(filename, content_type):
    with pytest.raises(ValueError):
        app_module._validate_upload_extension(filename, content_type)


def test_sanitized_disk_filename_strips_path_traversal():
    result = app_module._sanitized_disk_filename('../../etc/passwd.jpg', '.jpg')
    assert '..' not in result
    assert '/' not in result and '\\' not in result


def test_sanitized_disk_filename_strips_windows_path():
    result = app_module._sanitized_disk_filename('C:\\evil\\path\\photo.jpg', '.jpg')
    assert result == 'photo.jpg'


def test_display_filename_strips_control_characters():
    result = app_module._display_filename('photo\x00\x07.jpg')
    assert '\x00' not in result and '\x07' not in result


# ---------------------------------------------------------------------------
# Altitude reference handling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('ref,expected', [
    (1, True), (0, False), (1.0, True), ('Below Sea Level', True), ('Above Sea Level', False), (None, False),
])
def test_altitude_below_sea_level(ref, expected):
    assert app_module._altitude_below_sea_level(ref) is expected


# ---------------------------------------------------------------------------
# extract_gps: GPS signs, altitude, range validation
# ---------------------------------------------------------------------------

def test_extract_gps_basic_positive_coordinates(make_jpeg):
    path = make_jpeg(lat=45.5, lon=122.6)  # positive lon (east)
    features, errors = app_module.extract_gps([path], ['a.jpg'])
    assert errors == []
    assert len(features) == 1
    assert features[0]['latitude'] == pytest.approx(45.5, abs=1e-4)
    assert features[0]['longitude'] == pytest.approx(122.6, abs=1e-4)


def test_extract_gps_negative_hemisphere_signs(make_jpeg):
    path = make_jpeg(lat=-33.9, lon=-122.6)  # S latitude, W longitude
    features, errors = app_module.extract_gps([path], ['a.jpg'])
    assert errors == []
    assert features[0]['latitude'] < 0
    assert features[0]['longitude'] < 0


def test_extract_gps_altitude_above_sea_level(make_jpeg):
    path = make_jpeg(lat=45.5, lon=-122.6, alt=120.0, alt_below_sea_level=False)
    features, _ = app_module.extract_gps([path], ['a.jpg'])
    assert features[0]['altitude_m'] == pytest.approx(120.0, abs=0.1)
    assert features[0]['altitude_m'] > 0


def test_extract_gps_altitude_below_sea_level(make_jpeg):
    path = make_jpeg(lat=45.5, lon=-122.6, alt=25.0, alt_below_sea_level=True)
    features, _ = app_module.extract_gps([path], ['a.jpg'])
    assert features[0]['altitude_m'] == pytest.approx(-25.0, abs=0.1)


def test_extract_gps_missing_gps_reports_error_not_crash(make_jpeg):
    path = make_jpeg(lat=None, lon=None)
    features, errors = app_module.extract_gps([path], ['a.jpg'])
    assert features == []
    assert len(errors) == 1
    assert 'No GPS' in errors[0]['error']


def test_extract_gps_true_vs_magnetic_heading(make_jpeg):
    true_path = make_jpeg(name='true.jpg', heading=90.0, heading_ref='T')
    mag_path = make_jpeg(name='mag.jpg', heading=90.0, heading_ref='M')
    features, _ = app_module.extract_gps([true_path, mag_path], ['true.jpg', 'mag.jpg'])
    true_f = next(f for f in features if f['filename'] == 'true.jpg')
    mag_f = next(f for f in features if f['filename'] == 'mag.jpg')
    assert true_f['heading_is_true'] is True
    assert mag_f['heading_is_true'] is False


def test_extract_gps_mixed_batch_one_bad_does_not_drop_others(make_jpeg, corrupt_jpeg):
    good_path = make_jpeg(name='good.jpg')
    features, errors = app_module.extract_gps([good_path, corrupt_jpeg], ['good.jpg', 'corrupt.jpg'])
    assert any(f['filename'] == 'good.jpg' for f in features)
    # A structurally-broken file either still yields no GPS (reported as an
    # error) or ExifTool tolerates it -- either way the good file survives.
    assert len(features) >= 1


# ---------------------------------------------------------------------------
# HEIC preview generation
# ---------------------------------------------------------------------------

def test_build_heic_preview_produces_data_uri(make_heic):
    path = make_heic()
    preview = app_module._build_heic_preview(path)
    assert preview is not None
    assert preview.startswith('data:image/jpeg;base64,')


def test_build_heic_preview_returns_none_for_corrupt_file(corrupt_jpeg):
    # corrupt_jpeg isn't a HEIC file, but the function should degrade
    # gracefully (return None) for any undecodable input.
    preview = app_module._build_heic_preview(corrupt_jpeg)
    assert preview is None


def test_extract_gps_reads_heic_gps(make_heic):
    path = make_heic(lat=48.85, lon=2.35)
    features, errors = app_module.extract_gps([path], ['photo.heic'])
    assert errors == []
    assert features[0]['latitude'] == pytest.approx(48.85, abs=1e-3)


# ---------------------------------------------------------------------------
# End-to-end /upload smoke tests
# ---------------------------------------------------------------------------

def test_upload_mixed_jpeg_png_heic_batch(client, make_jpeg, make_png, make_heic):
    jpeg_path = make_jpeg(name='a.jpg', lat=10.0, lon=20.0)
    png_path = make_png(name='b.png', lat=11.0, lon=21.0)
    heic_path = make_heic(name='c.heic', lat=12.0, lon=22.0)

    files = [
        ('photos', ('a.jpg', open(jpeg_path, 'rb'), 'image/jpeg')),
        ('photos', ('b.png', open(png_path, 'rb'), 'image/png')),
        ('photos', ('c.heic', open(heic_path, 'rb'), 'image/heic')),
    ]
    try:
        res = client.post('/upload', files=files)
    finally:
        for _, (_, fh, _) in files:
            fh.close()

    assert res.status_code == 200
    body = res.json()
    assert body['total_uploaded'] == 3
    assert body['total_geotagged'] == 3
    assert 'upload_id' in body
    assert 'c.heic' in body['previews']
    assert 'a.jpg' not in body['previews']  # JPEG/PNG stay client-side


def test_upload_rejects_unsupported_extension(client, tmp_path):
    bogus = tmp_path / 'a.gif'
    bogus.write_bytes(b'GIF89a')
    with open(bogus, 'rb') as f:
        res = client.post('/upload', files={'photos': ('a.gif', f, 'image/gif')})
    assert res.status_code == 200
    body = res.json()
    assert body['total_geotagged'] == 0
    assert any('Unsupported file type' in e['error'] for e in body['errors'])


def test_upload_jpeg_png_regression_still_works(client, make_jpeg, make_png):
    jpeg_path = make_jpeg(lat=1.0, lon=2.0)
    png_path = make_png(lat=3.0, lon=4.0)
    with open(jpeg_path, 'rb') as jf, open(png_path, 'rb') as pf:
        res = client.post('/upload', files=[
            ('photos', ('photo.jpg', jf, 'image/jpeg')),
            ('photos', ('photo.png', pf, 'image/png')),
        ])
    assert res.status_code == 200
    body = res.json()
    assert body['total_geotagged'] == 2
    filenames = {f['properties']['filename'] for f in body['geojson']['features']}
    assert filenames == {'photo.jpg', 'photo.png'}
