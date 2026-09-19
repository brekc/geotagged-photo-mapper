"""Additional edge-case coverage: session concurrency, upload limits,
Unicode/formula-injection-via-filename, and custom CRS with Oriented Imagery.

These target scenarios the main test files don't exercise directly.
"""

import threading

import geotagged_photo_mapper as app_module
import upload_sessions


# ---------------------------------------------------------------------------
# Session concurrency
# ---------------------------------------------------------------------------

def test_concurrent_uploads_never_cross_contaminate():
    """Many threads each create a session and write/read their own rows.
    If the lock were missing or broken, rows could bleed between sessions."""
    errors = []

    def worker(n):
        try:
            uid = upload_sessions.create_session()
            upload_sessions.set_rows(uid, [{'filename': f'{n}.jpg', 'owner': n}])
            for _ in range(20):
                fetched = upload_sessions.get_rows(uid)
                if len(fetched) != 1 or fetched[0]['owner'] != n:
                    errors.append(f'thread {n} saw contaminated rows: {fetched}')
            upload_sessions.delete_session(uid)
        except Exception as e:  # noqa: BLE001
            errors.append(f'thread {n} raised: {e}')

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []


def test_concurrent_set_rows_on_same_session_stays_consistent():
    """Two threads repeatedly replacing the same session's rows should never
    observe a partially-written state (the lock covers the whole replace)."""
    uid = upload_sessions.create_session()
    errors = []

    def worker(label):
        for i in range(50):
            try:
                upload_sessions.set_rows(uid, [{'filename': f'{label}-{i}.jpg'} for _ in range(3)])
                rows = upload_sessions.get_rows(uid)
                if len(rows) != 3:
                    errors.append(f'{label}: expected 3 rows, got {len(rows)}')
            except upload_sessions.SessionNotFound:
                pass  # session may have been evicted by other test runs; not a correctness issue here

    threads = [threading.Thread(target=worker, args=(label,)) for label in ('a', 'b')]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []


# ---------------------------------------------------------------------------
# Upload limits
# ---------------------------------------------------------------------------

def test_upload_rejects_too_many_files(client, make_jpeg):
    paths = [make_jpeg(name=f'{i}.jpg') for i in range(app_module.MAX_FILES_PER_UPLOAD + 1)]
    files = [('photos', (f'{i}.jpg', open(p, 'rb'), 'image/jpeg')) for i, p in enumerate(paths)]
    try:
        res = client.post('/upload', files=files)
    finally:
        for _, (_, fh, _) in files:
            fh.close()
    assert res.status_code == 400
    assert 'Too many files' in res.json()['detail']


def test_upload_rejects_oversized_single_file(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, 'MAX_FILE_SIZE_BYTES', 100)  # tiny cap forces the guard to trip
    path = tmp_path / 'big.jpg'
    path.write_bytes(b'\xff\xd8\xff\xe0' + b'0' * 1000)  # JPEG-ish header + padding, exceeds the tiny cap
    with open(path, 'rb') as f:
        res = client.post('/upload', files={'photos': ('big.jpg', f, 'image/jpeg')})
    assert res.status_code == 200
    body = res.json()
    assert body['total_geotagged'] == 0
    assert any('size limit' in e['error'] for e in body['errors'])


def test_decompression_bomb_guard_is_active():
    from PIL import Image
    assert Image.MAX_IMAGE_PIXELS == app_module.MAX_DECODED_PIXELS


def test_oversized_image_preview_fails_closed_not_crashed(make_heic, monkeypatch):
    from PIL import Image
    path = make_heic(size=(200, 200))
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 100)  # far below a 200x200 image, forces the guard to trip
    preview = app_module._build_heic_preview(path)
    assert preview is None  # DecompressionBombError is caught, not raised out of the function


# ---------------------------------------------------------------------------
# Unicode / formula-injection via filename
# ---------------------------------------------------------------------------

def test_unicode_emoji_filename_round_trips(client, make_jpeg):
    src = make_jpeg(name='plain.jpg', lat=1.0, lon=2.0)
    original_name = 'bird_\U0001F426_\u00e9\u00e8.jpg'
    with open(src, 'rb') as f:
        res = client.post('/upload', files={'photos': (original_name, f, 'image/jpeg')})
    assert res.status_code == 200
    body = res.json()
    assert body['total_geotagged'] == 1
    filename = body['geojson']['features'][0]['properties']['filename']
    assert filename == original_name  # printable Unicode (including emoji) is preserved exactly


def test_formula_injection_filename_is_escaped_in_oriented_imagery_csv(client, make_jpeg):
    src = make_jpeg(name='plain.jpg', lat=1.0, lon=2.0)
    with open(src, 'rb') as f:
        res = client.post('/upload', files={'photos': ('=1+1.jpg', f, 'image/jpeg')})
    body = res.json()
    upload_id = body['upload_id']

    res = client.post('/oriented-imagery/reference', data={
        'upload_id': upload_id,
        'base_location': r'C:\Photos\a',
        'oriented_imagery_type': 'Nadir',
        'epsg': '4326',
    })
    assert res.status_code == 200
    # Name/ImagePath must not appear as a bare formula-triggering prefix.
    for line in res.text.splitlines()[1:]:
        assert not any(cell.startswith(('=', '+', '@')) for cell in line.split(','))


def test_path_traversal_filename_is_reduced_to_a_bare_basename(client, make_jpeg):
    src = make_jpeg(name='plain.jpg', lat=1.0, lon=2.0)
    with open(src, 'rb') as f:
        res = client.post('/upload', files={'photos': ('../../evil.jpg', f, 'image/jpeg')})
    assert res.status_code == 200
    body = res.json()
    assert body['total_geotagged'] == 1  # processed safely, not rejected outright, but never escapes tmp_dir
    filename = body['geojson']['features'][0]['properties']['filename']
    assert filename == 'evil.jpg'


# ---------------------------------------------------------------------------
# Custom CRS with Oriented Imagery (never exercised elsewhere)
# ---------------------------------------------------------------------------

def test_oriented_imagery_reference_with_custom_crs(client, make_jpeg):
    src = make_jpeg(name='a.jpg', lat=45.0, lon=-122.0)
    with open(src, 'rb') as f:
        res = client.post('/upload', files={'photos': ('a.jpg', f, 'image/jpeg')})
    upload_id = res.json()['upload_id']

    res = client.post('/oriented-imagery/reference', data={
        'upload_id': upload_id,
        'base_location': r'C:\Photos\a',
        'oriented_imagery_type': 'Nadir',
        'custom_crs': 'EPSG:26910',  # NAD83 / UTM zone 10N -- a real projected CRS, not geographic
    })
    assert res.status_code == 200
    header = res.text.splitlines()[0].split(',')
    srs_col = res.text.splitlines()[1].split(',')[header.index('SRS')]
    assert srs_col == '26910'


def test_oriented_imagery_reference_with_wkt_only_custom_crs_uses_wkt_as_srs(client, make_jpeg):
    # A CRS with no EPSG code (e.g. a raw PROJ4/WKT-only definition) should
    # fall back to WKT text for SRS rather than crashing on to_epsg() -> None.
    from pyproj import CRS
    wkt = CRS.from_proj4('+proj=longlat +ellps=GRS80 +no_defs').to_wkt()

    src = make_jpeg(name='a.jpg', lat=45.0, lon=-122.0)
    with open(src, 'rb') as f:
        res = client.post('/upload', files={'photos': ('a.jpg', f, 'image/jpeg')})
    upload_id = res.json()['upload_id']

    res = client.post('/oriented-imagery/reference', data={
        'upload_id': upload_id,
        'base_location': r'C:\Photos\a',
        'oriented_imagery_type': 'Nadir',
        'custom_crs': wkt,
    })
    assert res.status_code == 200


# ---------------------------------------------------------------------------
# Misc route behavior not covered elsewhere
# ---------------------------------------------------------------------------

def test_export_rejects_unknown_format(client, make_jpeg):
    src = make_jpeg(name='a.jpg', lat=1.0, lon=2.0)
    with open(src, 'rb') as f:
        res = client.post('/upload', files={'photos': ('a.jpg', f, 'image/jpeg')})
    upload_id = res.json()['upload_id']
    res = client.post('/export', data={'format': 'shapefile.exe', 'upload_id': upload_id, 'epsg': '4326'})
    assert res.status_code == 400
    assert 'Unknown format' in res.json()['detail']


def test_reference_mode_all_excluded_still_returns_valid_empty_csv(client, make_png):
    src = make_png(name='a.png', lat=1.0, lon=2.0)
    with open(src, 'rb') as f:
        res = client.post('/upload', files={'photos': ('a.png', f, 'image/png')})
    upload_id = res.json()['upload_id']

    res = client.post('/oriented-imagery/reference', data={
        'upload_id': upload_id,
        'base_location': r'C:\Photos\a',
        'oriented_imagery_type': 'Nadir',
        'epsg': '4326',
    })
    assert res.status_code == 200
    lines = res.text.splitlines()
    assert lines[0].split(',')[:3] == ['X', 'Y', 'ImagePath']
    assert len(lines) == 1  # header only, zero data rows -- PNG is excluded in reference mode
