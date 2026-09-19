"""Unit tests for the isolated upload-session store (upload_sessions.py)."""

import pytest

import upload_sessions as sessions


@pytest.fixture(autouse=True)
def _clean_store():
    # The module keeps process-global state; give every test a clean slate
    # and clean up afterward so tests don't leak into each other.
    sessions._SESSIONS.clear()
    yield
    sessions._SESSIONS.clear()


def test_create_session_returns_unique_random_ids():
    ids = {sessions.create_session() for _ in range(20)}
    assert len(ids) == 20
    for uid in ids:
        assert len(uid) >= 32


def test_set_rows_assigns_unique_random_row_ids():
    uid = sessions.create_session()
    stored = sessions.set_rows(uid, [{'filename': 'a.jpg'}, {'filename': 'b.jpg'}])
    row_ids = [r['row_id'] for r in stored]
    assert len(set(row_ids)) == 2
    for rid in row_ids:
        assert len(rid) >= 16


def test_get_rows_returns_defensive_copies():
    uid = sessions.create_session()
    sessions.set_rows(uid, [{'filename': 'a.jpg'}])
    rows = sessions.get_rows(uid)
    rows[0]['filename'] = 'tampered.jpg'
    rows_again = sessions.get_rows(uid)
    assert rows_again[0]['filename'] == 'a.jpg'


def test_unknown_upload_id_fails_closed():
    with pytest.raises(sessions.SessionNotFound):
        sessions.get_rows('does-not-exist')
    with pytest.raises(sessions.SessionNotFound):
        sessions.set_rows('does-not-exist', [{'filename': 'a.jpg'}])


def test_deleted_session_fails_closed():
    uid = sessions.create_session()
    sessions.set_rows(uid, [{'filename': 'a.jpg'}])
    sessions.delete_session(uid)
    with pytest.raises(sessions.SessionNotFound):
        sessions.get_rows(uid)


def test_delete_session_is_idempotent():
    sessions.delete_session('never-existed')  # must not raise
    uid = sessions.create_session()
    sessions.delete_session(uid)
    sessions.delete_session(uid)  # second delete is a no-op, not an error


def test_stale_or_cross_session_row_ids_are_dropped_not_erroring():
    uid_a = sessions.create_session()
    rows_a = sessions.set_rows(uid_a, [{'filename': 'a.jpg'}])
    uid_b = sessions.create_session()
    sessions.set_rows(uid_b, [{'filename': 'b.jpg'}])

    # Asking session A for a row_id that belongs to session B (or doesn't
    # exist at all) returns nothing for that id rather than leaking it.
    result = sessions.get_rows(uid_a, row_ids=[rows_a[0]['row_id'], 'not-a-real-row-id'])
    assert len(result) == 1
    assert result[0]['filename'] == 'a.jpg'


def test_session_expires_after_ttl(monkeypatch):
    fake_time = [1000.0]
    monkeypatch.setattr(sessions, '_now', lambda: fake_time[0])

    uid = sessions.create_session()
    sessions.set_rows(uid, [{'filename': 'a.jpg'}])

    fake_time[0] += sessions.SESSION_TTL_SECONDS + 1
    with pytest.raises(sessions.SessionNotFound):
        sessions.get_rows(uid)


def test_sliding_expiration_is_extended_by_access(monkeypatch):
    fake_time = [1000.0]
    monkeypatch.setattr(sessions, '_now', lambda: fake_time[0])

    uid = sessions.create_session()
    sessions.set_rows(uid, [{'filename': 'a.jpg'}])

    # Access just before expiry should reset the clock.
    fake_time[0] += sessions.SESSION_TTL_SECONDS - 1
    sessions.get_rows(uid)

    fake_time[0] += sessions.SESSION_TTL_SECONDS - 1
    # Still alive because the access above reset the sliding window.
    sessions.get_rows(uid)


def test_max_rows_per_session_enforced():
    uid = sessions.create_session()
    too_many = [{'filename': f'{i}.jpg'} for i in range(sessions.MAX_ROWS_PER_SESSION + 1)]
    with pytest.raises(sessions.SessionLimitExceeded):
        sessions.set_rows(uid, too_many)


def test_max_sessions_evicts_oldest(monkeypatch):
    fake_time = [1000.0]
    monkeypatch.setattr(sessions, '_now', lambda: fake_time[0])

    first_uid = sessions.create_session()
    for _ in range(sessions.MAX_SESSIONS):
        fake_time[0] += 1
        sessions.create_session()

    # The very first session should have been evicted to make room.
    with pytest.raises(sessions.SessionNotFound):
        sessions.get_rows(first_uid)
