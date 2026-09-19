"""Tests for the Oriented Imagery table builder (oriented_imagery.py)."""

import csv
import io
import json
import zipfile

import pytest
from PIL import Image

import oriented_imagery as oi


def _base_meta(**overrides):
    meta = {
        'filename': 'IMG_0001.jpg',
        'latitude': 45.5,
        'longitude': -122.6,
        'altitude_m': None,
        'datetime': None,
        'camera_make': None,
        'camera_model': None,
        'heading_deg': None,
        'heading_is_true': False,
        'focal_length_mm': None,
        'focal_length_35mm_eq': None,
        'orientation': 1,
        'pixel_width': 4000,
        'pixel_height': 3000,
        'subsec_time_original': None,
        'offset_time_original': None,
        'vendor_pose_detected': False,
    }
    meta.update(overrides)
    return meta


# ---------------------------------------------------------------------------
# Field ordering / required fields
# ---------------------------------------------------------------------------

def test_field_order_is_stable_and_documented():
    assert oi.ORIENTED_IMAGERY_FIELDS[:3] == ['X', 'Y', 'ImagePath']
    assert oi.ORIENTED_IMAGERY_FIELDS == oi.CORE_FIELDS + oi.AUDIT_FIELDS
    # SequenceOrder is the last core (Esri-documented) field.
    assert oi.CORE_FIELDS[-1] == 'SequenceOrder'


def test_csv_header_matches_field_order():
    csv_text = oi.write_oriented_imagery_csv([])
    header = next(csv.reader(io.StringIO(csv_text)))
    assert header == oi.ORIENTED_IMAGERY_FIELDS


def test_row_without_gps_is_excluded():
    result = oi.build_row(_base_meta(latitude=None, longitude=None), 'a.jpg', 'a.jpg', '4326', 'Nadir', 1, False)
    assert result.row is None
    assert result.status == 'excluded'


def test_minimum_row_has_x_y_imagepath_and_blank_pose_fields():
    result = oi.build_row(_base_meta(), 'a.jpg', '/photos/a.jpg', '4326', 'Nadir', 1, False)
    row = result.row
    assert row['X'] == -122.6
    assert row['Y'] == 45.5
    assert row['ImagePath'] == '/photos/a.jpg'
    assert result.status == 'minimum-only'
    for field in ('CameraPitch', 'CameraRoll', 'Omega', 'Phi', 'Kappa', 'Matrix',
                  'PrincipalX', 'PrincipalY', 'Radial', 'Tangential',
                  'A0', 'A1', 'A2', 'B0', 'B1', 'B2'):
        assert row[field] == ''


# ---------------------------------------------------------------------------
# Heading: true vs magnetic
# ---------------------------------------------------------------------------

def test_true_heading_is_used():
    meta = _base_meta(heading_deg=87.5, heading_is_true=True)
    result = oi.build_row(meta, 'a.jpg', 'a.jpg', '4326', 'Horizontal', 1, False)
    assert result.row['CameraHeading'] == 87.5
    assert 'magnetic_heading_unsupported' not in result.warnings


def test_magnetic_heading_is_blanked_with_warning():
    meta = _base_meta(heading_deg=87.5, heading_is_true=False)
    result = oi.build_row(meta, 'a.jpg', 'a.jpg', '4326', 'Horizontal', 1, False)
    assert result.row['CameraHeading'] == ''
    assert 'magnetic_heading_unsupported' in result.warnings


# ---------------------------------------------------------------------------
# Altitude / datum warning
# ---------------------------------------------------------------------------

def test_altitude_present_always_warns_unknown_datum():
    meta = _base_meta(altitude_m=123.4)
    result = oi.build_row(meta, 'a.jpg', 'a.jpg', '4326', 'Nadir', 1, False)
    assert result.row['Z'] == 123.4
    assert 'unknown_altitude_datum' in result.warnings


def test_no_altitude_means_blank_z_and_no_datum_warning():
    result = oi.build_row(_base_meta(altitude_m=None), 'a.jpg', 'a.jpg', '4326', 'Nadir', 1, False)
    assert result.row['Z'] == ''
    assert 'unknown_altitude_datum' not in result.warnings


# ---------------------------------------------------------------------------
# AcquisitionDate / timezone
# ---------------------------------------------------------------------------

def test_acquisition_date_without_timezone_warns_and_does_not_invent_utc():
    meta = _base_meta(datetime='2024:06:15 14:32:10')
    result = oi.build_row(meta, 'a.jpg', 'a.jpg', '4326', 'Nadir', 1, False)
    assert result.row['AcquisitionDate'] == '2024:06:15 14:32:10'
    assert 'Z' not in result.row['AcquisitionDate']
    assert 'missing_timezone' in result.warnings


def test_acquisition_date_with_timezone_and_subsecond():
    meta = _base_meta(datetime='2024:06:15 14:32:10', subsec_time_original='437', offset_time_original='-05:00')
    result = oi.build_row(meta, 'a.jpg', 'a.jpg', '4326', 'Nadir', 1, False)
    assert result.row['AcquisitionDate'] == '2024:06:15 14:32:10.437-05:00'
    assert 'missing_timezone' not in result.warnings


# ---------------------------------------------------------------------------
# ImageRotation / EXIF orientation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('orientation,expected_rotation,expect_mirror_warning', [
    (1, 0, False), (3, 180, False), (6, 270, False), (8, 90, False),
    (2, 0, True), (4, 180, True), (5, 270, True), (7, 90, True),
])
def test_orientation_to_rotation_all_eight_values(orientation, expected_rotation, expect_mirror_warning):
    meta = _base_meta(orientation=orientation)
    result = oi.build_row(meta, 'a.jpg', 'a.jpg', '4326', 'Nadir', 1, pixels_normalized=False)
    assert result.row['ImageRotation'] == expected_rotation
    assert ('mirrored_orientation_unsupported' in result.warnings) == expect_mirror_warning


def test_portable_mode_forces_zero_rotation_regardless_of_orientation():
    meta = _base_meta(orientation=6)
    result = oi.build_row(meta, 'a.jpg', 'a.jpg', '4326', 'Nadir', 1, pixels_normalized=True)
    assert result.row['ImageRotation'] == 0


# ---------------------------------------------------------------------------
# Focal length / FOV
# ---------------------------------------------------------------------------

def test_fov_estimated_for_landscape_labels_as_approximate():
    meta = _base_meta(focal_length_35mm_eq=28, pixel_width=4000, pixel_height=3000, orientation=1)
    result = oi.build_row(meta, 'a.jpg', 'a.jpg', '4326', 'Nadir', 1, False)
    assert result.row['HorizontalFieldOfView'] > result.row['VerticalFieldOfView']
    assert 'approximate_fov_35mm_equivalent' in result.warnings


def test_fov_swapped_for_portrait_images():
    meta = _base_meta(focal_length_35mm_eq=28, pixel_width=3000, pixel_height=4000, orientation=1)
    result = oi.build_row(meta, 'a.jpg', 'a.jpg', '4326', 'Nadir', 1, False)
    assert result.row['VerticalFieldOfView'] > result.row['HorizontalFieldOfView']


def test_no_focal_length_means_no_fov():
    result = oi.build_row(_base_meta(), 'a.jpg', 'a.jpg', '4326', 'Nadir', 1, False)
    assert result.row['HorizontalFieldOfView'] == ''
    assert result.row['VerticalFieldOfView'] == ''


# ---------------------------------------------------------------------------
# Vendor pose metadata
# ---------------------------------------------------------------------------

def test_vendor_pose_detected_warns_without_populating_pose_fields():
    meta = _base_meta(vendor_pose_detected=True)
    result = oi.build_row(meta, 'a.jpg', 'a.jpg', '4326', 'Nadir', 1, False)
    assert 'vendor_pose_metadata_unsupported' in result.warnings
    assert result.row['Omega'] == '' and result.row['Phi'] == '' and result.row['Kappa'] == ''


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------

def test_complete_status_requires_date_heading_and_fov():
    meta = _base_meta(datetime='2024:06:15 14:32:10', offset_time_original='-05:00',
                       heading_deg=10, heading_is_true=True, focal_length_35mm_eq=28)
    result = oi.build_row(meta, 'a.jpg', 'a.jpg', '4326', 'Nadir', 1, False)
    assert result.status == 'complete'


def test_partial_status_with_only_one_extra_field():
    meta = _base_meta(datetime='2024:06:15 14:32:10')
    result = oi.build_row(meta, 'a.jpg', 'a.jpg', '4326', 'Nadir', 1, False)
    assert result.status == 'partial'


# ---------------------------------------------------------------------------
# CSV formula-injection safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('dangerous', ['=cmd|calc', '+1+1', '-1+1', '@SUM(A1)', '\ttab'])
def test_csv_safe_text_escapes_dangerous_prefixes(dangerous):
    assert oi.csv_safe_text(dangerous).startswith("'")


def test_csv_safe_text_leaves_ordinary_text_alone():
    assert oi.csv_safe_text('IMG_0001.jpg') == 'IMG_0001.jpg'


def test_negative_coordinate_is_never_quoted_in_csv():
    row = {f: '' for f in oi.ORIENTED_IMAGERY_FIELDS}
    row['X'] = -122.6
    row['Y'] = 45.5
    row['ImagePath'] = 'a.jpg'
    csv_text = oi.write_oriented_imagery_csv([row])
    data_row = list(csv.reader(io.StringIO(csv_text)))[1]
    x_value = data_row[oi.ORIENTED_IMAGERY_FIELDS.index('X')]
    assert x_value == '-122.6'
    assert not x_value.startswith("'")


def test_malicious_make_field_is_escaped_in_csv():
    meta = _base_meta(camera_make='=HYPERLINK("http://evil")')
    result = oi.build_row(meta, 'a.jpg', 'a.jpg', '4326', 'Nadir', 1, False)
    csv_text = oi.write_oriented_imagery_csv([result.row])
    assert "'=HYPERLINK" in csv_text


# ---------------------------------------------------------------------------
# Mode A: base location validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('good', [
    r'C:\Photos\project',
    r'\\server\share\photos',
    'https://example.com/photos',
    'http://example.com/photos/',
])
def test_valid_base_locations_accepted(good):
    kind, normalized = oi.validate_base_location(good)
    assert kind in ('local', 'unc', 'url')
    assert normalized.endswith(('\\', '/'))


@pytest.mark.parametrize('bad', [
    '',
    'javascript:alert(1)',
    'ftp://example.com/photos',
    'file:///etc/passwd',
    'http://user:pass@example.com/photos',
    'C:\\Photos\\..\\..\\Windows',
    'relative/path/no/root',
    'http://',
    'C:\\Photos\\bad\x00name',
    'x' * 600,
])
def test_invalid_base_locations_rejected(bad):
    with pytest.raises(oi.InvalidBaseLocation):
        oi.validate_base_location(bad)


def test_reference_export_excludes_png_and_heic_with_warning():
    rows = [
        _base_meta(filename='a.jpg'),
        _base_meta(filename='b.png'),
        _base_meta(filename='c.heic'),
        _base_meta(filename='d.tif'),
    ]
    result = oi.build_reference_export(rows, r'C:\Photos\project', '4326', 'Nadir')
    assert result['row_count'] == 2
    assert result['excluded_count'] == 2
    warned_files = {w['filename'] for w in result['warnings']}
    assert 'b.png' in warned_files and 'c.heic' in warned_files


def test_reference_export_never_claims_verification():
    rows = [_base_meta()]
    result = oi.build_reference_export(rows, r'C:\Photos\project', '4326', 'Nadir')
    assert 'cannot confirm' in result['note'].lower()


def test_reference_export_image_path_is_joined_with_base():
    rows = [_base_meta(filename='a.jpg')]
    result = oi.build_reference_export(rows, r'C:\Photos\project', '4326', 'Nadir')
    assert result['preview_paths'][0] == r'C:\Photos\project\a.jpg'


# ---------------------------------------------------------------------------
# Mode B: portable package
# ---------------------------------------------------------------------------

def _write_plain_jpeg(path, size=(40, 30), color=(10, 20, 30)):
    Image.new('RGB', size, color).save(path, format='JPEG')


def test_portable_package_produces_expected_members(tmp_path):
    p1 = tmp_path / 'a.jpg'
    _write_plain_jpeg(str(p1))
    items = [oi.PortableItem(display_name='a.jpg', source_path=str(p1))]
    rows_meta = [_base_meta(filename='a.jpg')]

    zip_bytes = oi.build_portable_package(items, rows_meta, '4326', 'Nadir', 'oriented_imagery')
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        assert 'oriented_imagery.csv' in names
        assert 'manifest.json' in names
        assert 'README.txt' in names
        assert any(n.startswith('images/') and n.endswith('.jpg') for n in names)

        # No unsafe zip members: no backslashes, absolute paths, or traversal.
        for n in names:
            assert '\\' not in n
            assert not n.startswith('/')
            assert '..' not in n.split('/')

        manifest = json.loads(zf.read('manifest.json'))
        assert manifest['row_count'] == 1
        assert manifest['images'][0]['source_sha256']
        assert manifest['images'][0]['derivative_sha256']
        assert manifest['images'][0]['source_sha256'] != manifest['images'][0]['derivative_sha256']


def test_portable_derivative_strips_exif(tmp_path):
    p1 = tmp_path / 'a.jpg'
    img = Image.new('RGB', (40, 30), (10, 20, 30))
    exif = img.getexif()
    exif[0x0110] = 'Sneaky Camera Model'  # Model tag
    img.save(str(p1), format='JPEG', exif=exif)

    items = [oi.PortableItem(display_name='a.jpg', source_path=str(p1))]
    rows_meta = [_base_meta(filename='a.jpg')]
    zip_bytes = oi.build_portable_package(items, rows_meta, '4326', 'Nadir', 'oriented_imagery')

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        deriv_name = next(n for n in zf.namelist() if n.startswith('images/'))
        deriv_bytes = zf.read(deriv_name)

    deriv_img = Image.open(io.BytesIO(deriv_bytes))
    assert deriv_img.getexif() in (None, {}) or len(deriv_img.getexif()) == 0


def test_derivative_filenames_are_deterministic_and_collision_safe():
    a = oi.derivative_filename(1, 'IMG_0001.HEIC')
    b = oi.derivative_filename(2, 'IMG_0001.JPG')
    assert a != b
    assert a == oi.derivative_filename(1, 'IMG_0001.HEIC')  # deterministic


def test_portable_package_enforces_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(oi, 'MAX_PORTABLE_ZIP_BYTES', 100)  # tiny cap forces the guard to trip
    p1 = tmp_path / 'a.jpg'
    _write_plain_jpeg(str(p1), size=(200, 200))
    items = [oi.PortableItem(display_name='a.jpg', source_path=str(p1))]
    rows_meta = [_base_meta(filename='a.jpg')]
    with pytest.raises(oi.PortablePackageTooLarge):
        oi.build_portable_package(items, rows_meta, '4326', 'Nadir', 'oriented_imagery')


def test_portable_package_skips_files_without_gps(tmp_path):
    p1 = tmp_path / 'a.jpg'
    _write_plain_jpeg(str(p1))
    items = [oi.PortableItem(display_name='a.jpg', source_path=str(p1))]
    rows_meta = [_base_meta(filename='a.jpg', latitude=None, longitude=None)]
    zip_bytes = oi.build_portable_package(items, rows_meta, '4326', 'Nadir', 'oriented_imagery')
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        manifest = json.loads(zf.read('manifest.json'))
        assert manifest['row_count'] == 0
        assert any(w['warning'] == 'no_gps_data' for w in manifest['excluded'])


# ---------------------------------------------------------------------------
# Preflight counts
# ---------------------------------------------------------------------------

def test_preflight_counts():
    rows = [
        _base_meta(filename='a.jpg', datetime='2024:01:01 00:00:00', heading_is_true=True, focal_length_mm=4.2),
        _base_meta(filename='b.heic', latitude=None, longitude=None),
    ]
    preflight = oi.build_preflight(rows)
    assert preflight['total_files'] == 2
    assert preflight['valid_gps'] == 1
    assert preflight['excluded_files'] == 1
    assert preflight['files_requiring_conversion'] == 1


# ---------------------------------------------------------------------------
# Route-level integration: the full HTTP surface, not just the builder functions
# ---------------------------------------------------------------------------

def _upload(client, make_jpeg, name, lat, lon):
    path = make_jpeg(name=name, lat=lat, lon=lon)
    with open(path, 'rb') as f:
        res = client.post('/upload', files={'photos': (name, f, 'image/jpeg')})
    assert res.status_code == 200
    return res.json()


def test_reference_route_requires_explicit_type(client, make_jpeg):
    body = _upload(client, make_jpeg, 'a.jpg', 1.0, 2.0)
    # Omitted entirely: FastAPI's own required-Form-field validation rejects it (422).
    res = client.post('/oriented-imagery/reference', data={
        'upload_id': body['upload_id'],
        'base_location': r'C:\Photos\a',
        'epsg': '4326',
    })
    assert res.status_code == 422

    # Present but not one of the five documented types: our own explicit-selection check rejects it (400).
    res = client.post('/oriented-imagery/reference', data={
        'upload_id': body['upload_id'],
        'base_location': r'C:\Photos\a',
        'oriented_imagery_type': 'Vertical',
        'epsg': '4326',
    })
    assert res.status_code == 400


def test_reference_preview_route(client, make_jpeg):
    body = _upload(client, make_jpeg, 'a.jpg', 1.0, 2.0)
    res = client.post('/oriented-imagery/reference-preview', data={
        'upload_id': body['upload_id'],
        'base_location': r'C:\Photos\a',
        'oriented_imagery_type': 'Nadir',
        'epsg': '4326',
    })
    assert res.status_code == 200
    data = res.json()
    assert data['row_count'] == 1
    assert 'cannot confirm' in data['note'].lower()


def test_reference_route_rejects_unsafe_base_location(client, make_jpeg):
    body = _upload(client, make_jpeg, 'a.jpg', 1.0, 2.0)
    res = client.post('/oriented-imagery/reference', data={
        'upload_id': body['upload_id'],
        'base_location': 'javascript:alert(1)',
        'oriented_imagery_type': 'Nadir',
        'epsg': '4326',
    })
    assert res.status_code == 400


def test_portable_route_end_to_end(client, make_jpeg):
    body = _upload(client, make_jpeg, 'a.jpg', 1.0, 2.0)
    path = make_jpeg(name='a.jpg', lat=1.0, lon=2.0)
    with open(path, 'rb') as f:
        res = client.post('/oriented-imagery/portable', data={
            'upload_id': body['upload_id'],
            'oriented_imagery_type': 'Nadir',
            'epsg': '4326',
            'export_name': 'oi_test',
        }, files={'photos': ('a.jpg', f, 'image/jpeg')})
    assert res.status_code == 200
    assert res.headers['content-type'] == 'application/zip'
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        names = zf.namelist()
        assert 'oriented_imagery.csv' in names
        assert 'manifest.json' in names
        assert any(n.startswith('images/') for n in names)


def test_portable_route_requires_valid_session(client, make_jpeg):
    path = make_jpeg(name='a.jpg', lat=1.0, lon=2.0)
    with open(path, 'rb') as f:
        res = client.post('/oriented-imagery/portable', data={
            'upload_id': 'not-a-real-session',
            'oriented_imagery_type': 'Nadir',
            'epsg': '4326',
        }, files={'photos': ('a.jpg', f, 'image/jpeg')})
    assert res.status_code == 404
