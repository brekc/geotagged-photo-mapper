"""End-to-end multi-user isolation tests, driving the real FastAPI routes.

Simulates two independent "users" (browser tabs) uploading concurrently on
the same trusted LAN and asserts neither can read, export, or otherwise
affect the other's data.
"""

import upload_sessions


def _upload(client, make_jpeg, name, lat, lon):
    path = make_jpeg(name=name, lat=lat, lon=lon)
    with open(path, 'rb') as f:
        res = client.post('/upload', files={'photos': (name, f, 'image/jpeg')})
    assert res.status_code == 200
    return res.json()


def test_two_uploads_get_different_upload_ids(client, make_jpeg):
    a = _upload(client, make_jpeg, 'a.jpg', 1.0, 2.0)
    b = _upload(client, make_jpeg, 'b.jpg', 3.0, 4.0)
    assert a['upload_id'] != b['upload_id']


def test_export_requires_matching_upload_id(client, make_jpeg):
    a = _upload(client, make_jpeg, 'a.jpg', 1.0, 2.0)
    b = _upload(client, make_jpeg, 'b.jpg', 3.0, 4.0)

    # User A tries to export using User B's upload_id-shaped-but-foreign id:
    # since ids are random and unguessable, simulate a wrong/unknown id.
    res = client.post('/export', data={'format': 'csv', 'upload_id': 'not-a-real-session', 'epsg': '4326'})
    assert res.status_code == 404

    # Each user's own id still works and returns only their own row.
    res_a = client.post('/export', data={'format': 'csv', 'upload_id': a['upload_id'], 'epsg': '4326'})
    assert res_a.status_code == 200
    assert 'a.jpg' in res_a.text and 'b.jpg' not in res_a.text

    res_b = client.post('/export', data={'format': 'csv', 'upload_id': b['upload_id'], 'epsg': '4326'})
    assert res_b.status_code == 200
    assert 'b.jpg' in res_b.text and 'a.jpg' not in res_b.text


def test_deleted_session_is_unusable_afterward(client, make_jpeg):
    a = _upload(client, make_jpeg, 'a.jpg', 1.0, 2.0)
    res = client.delete(f"/session/{a['upload_id']}")
    assert res.status_code == 200

    res = client.post('/export', data={'format': 'csv', 'upload_id': a['upload_id'], 'epsg': '4326'})
    assert res.status_code == 404


def test_close_endpoint_is_idempotent_and_beacon_compatible(client, make_jpeg):
    a = _upload(client, make_jpeg, 'a.jpg', 1.0, 2.0)
    res1 = client.post(f"/session/{a['upload_id']}/close")
    res2 = client.post(f"/session/{a['upload_id']}/close")  # already gone -- still OK
    assert res1.status_code == 200
    assert res2.status_code == 200


def test_marker_removal_changes_export_contents(client, make_jpeg):
    path_a = make_jpeg(name='a.jpg', lat=1.0, lon=2.0)
    path_b = make_jpeg(name='b.jpg', lat=3.0, lon=4.0)
    with open(path_a, 'rb') as fa, open(path_b, 'rb') as fb:
        res = client.post('/upload', files=[
            ('photos', ('a.jpg', fa, 'image/jpeg')),
            ('photos', ('b.jpg', fb, 'image/jpeg')),
        ])
    body = res.json()
    upload_id = body['upload_id']
    row_ids = {f['properties']['filename']: f['properties']['row_id'] for f in body['geojson']['features']}
    assert set(row_ids) == {'a.jpg', 'b.jpg'}

    # Export with both rows visible.
    res_both = client.post('/export', data={'format': 'csv', 'upload_id': upload_id, 'epsg': '4326'})
    assert 'a.jpg' in res_both.text and 'b.jpg' in res_both.text

    # "Remove" b.jpg's marker: only send a.jpg's row_id.
    res_one = client.post('/export', data={
        'format': 'csv', 'upload_id': upload_id, 'epsg': '4326', 'row_ids': row_ids['a.jpg'],
    })
    assert 'a.jpg' in res_one.text and 'b.jpg' not in res_one.text


def test_unknown_row_id_alone_yields_no_data_error(client, make_jpeg):
    a = _upload(client, make_jpeg, 'a.jpg', 1.0, 2.0)
    res = client.post('/export', data={
        'format': 'csv', 'upload_id': a['upload_id'], 'epsg': '4326', 'row_ids': 'not-a-real-row-id',
    })
    assert res.status_code == 400


def test_session_state_stores_no_raw_bytes_or_paths(client, make_jpeg):
    a = _upload(client, make_jpeg, 'a.jpg', 1.0, 2.0)
    rows = upload_sessions.get_rows(a['upload_id'])
    assert len(rows) == 1
    row = rows[0]
    # Only normalized metadata should ever be present -- never a filesystem
    # path or raw photo bytes.
    for key, value in row.items():
        if isinstance(value, str):
            assert not any(s in value for s in ('.jpg.tmp', 'AppData', 'Temp\\'))
    assert isinstance(row.get('latitude'), float)


def test_oriented_imagery_export_requires_valid_session(client, make_jpeg):
    res = client.post('/oriented-imagery/preflight', data={'upload_id': 'not-a-real-session'})
    assert res.status_code == 404

    a = _upload(client, make_jpeg, 'a.jpg', 1.0, 2.0)
    res = client.post('/oriented-imagery/preflight', data={'upload_id': a['upload_id']})
    assert res.status_code == 200
    assert res.json()['total_files'] == 1


def test_oriented_imagery_reference_export_isolated_per_user(client, make_jpeg):
    a = _upload(client, make_jpeg, 'a.jpg', 1.0, 2.0)
    b = _upload(client, make_jpeg, 'b.jpg', 3.0, 4.0)

    res_a = client.post('/oriented-imagery/reference', data={
        'upload_id': a['upload_id'],
        'base_location': r'C:\Photos\a',
        'oriented_imagery_type': 'Nadir',
        'epsg': '4326',
        'export_name': 'oi',
    })
    assert res_a.status_code == 200
    assert 'a.jpg' in res_a.text and 'b.jpg' not in res_a.text

    # Trying to use A's own row_ids against B's upload_id must not leak A's data into B's export.
    row_id_a = upload_sessions.get_rows(a['upload_id'])[0]['row_id']
    res_cross = client.post('/oriented-imagery/reference', data={
        'upload_id': b['upload_id'],
        'row_ids': row_id_a,
        'base_location': r'C:\Photos\b',
        'oriented_imagery_type': 'Nadir',
        'epsg': '4326',
        'export_name': 'oi',
    })
    assert res_cross.status_code == 400  # A's row_id doesn't exist in B's session -> no matching rows
