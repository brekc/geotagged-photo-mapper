"""upload_sessions.py

Isolated, in-memory upload sessions so multiple users on the same trusted LAN
never read or overwrite each other's mapped photos.

Each browser upload gets its own opaque, cryptographically random
`upload_id`. A session holds only normalized metadata rows (the same
primitive fields the map and exports already use, each with its own random
`row_id`) plus per-row warnings -- never raw photo bytes or filesystem paths.
A single process-wide lock guards the store; a 15-minute sliding inactivity
window and hard caps on session/row counts bound memory.

This is an in-memory, process-local store, so it is only correct behind a
single Uvicorn worker. A multi-worker or multi-process deployment needs a
shared external store (Redis, a database) instead -- otherwise a session
created on one worker is invisible to a request routed to another.
"""

import secrets
import threading
import time

_LOCK = threading.Lock()
_SESSIONS: dict[str, dict] = {}

SESSION_TTL_SECONDS = 15 * 60
MAX_SESSIONS = 200
MAX_ROWS_PER_SESSION = 2000
MAX_TOTAL_ROWS = 20000


class SessionError(Exception):
    """Base class for session failures. Every caller must fail closed on these."""


class SessionNotFound(SessionError):
    pass


class SessionLimitExceeded(SessionError):
    pass


def _now() -> float:
    return time.monotonic()


def _is_expired(session: dict, now: float) -> bool:
    return (now - session['last_access']) > SESSION_TTL_SECONDS


def _prune_expired_locked(now: float) -> None:
    expired = [uid for uid, s in _SESSIONS.items() if _is_expired(s, now)]
    for uid in expired:
        del _SESSIONS[uid]


def _total_rows_locked() -> int:
    return sum(len(s['rows']) for s in _SESSIONS.values())


def _evict_oldest_locked() -> None:
    # Bounded storage: drop the least-recently-accessed session to make room
    # instead of growing without limit.
    if not _SESSIONS:
        return
    oldest_id = min(_SESSIONS, key=lambda uid: _SESSIONS[uid]['last_access'])
    del _SESSIONS[oldest_id]


def _get_locked(upload_id: str, now: float) -> dict:
    _prune_expired_locked(now)
    session = _SESSIONS.get(upload_id)
    if session is None:
        raise SessionNotFound('Unknown or expired upload session.')
    session['last_access'] = now
    return session


def create_session() -> str:
    """Start a new, empty session and return its opaque upload_id."""
    upload_id = secrets.token_urlsafe(32)
    now = _now()
    with _LOCK:
        _prune_expired_locked(now)
        while len(_SESSIONS) >= MAX_SESSIONS:
            _evict_oldest_locked()
        _SESSIONS[upload_id] = {
            'created_at': now,
            'last_access': now,
            'rows': {},       # row_id -> normalized feature dict
            'row_order': [],  # row_ids in upload order, for SequenceOrder
        }
    return upload_id


def set_rows(upload_id: str, features: list[dict]) -> list[dict]:
    """Replace a session's rows with freshly extracted features.

    Returns defensive copies of the features with a `row_id` merged into
    each one. Raises SessionNotFound for an unknown/expired id and
    SessionLimitExceeded if the row-count bounds would be exceeded.
    """
    now = _now()
    with _LOCK:
        session = _get_locked(upload_id, now)

        other_rows = _total_rows_locked() - len(session['rows'])
        if len(features) > MAX_ROWS_PER_SESSION:
            raise SessionLimitExceeded(f'Too many photos in one upload (max {MAX_ROWS_PER_SESSION}).')
        if other_rows + len(features) > MAX_TOTAL_ROWS:
            raise SessionLimitExceeded('Server-wide photo limit reached; try again shortly.')

        rows: dict[str, dict] = {}
        row_order: list[str] = []
        result: list[dict] = []
        for feature in features:
            row_id = secrets.token_urlsafe(16)
            row = dict(feature)
            row['row_id'] = row_id
            rows[row_id] = row
            row_order.append(row_id)
            result.append(dict(row))

        session['rows'] = rows
        session['row_order'] = row_order
        return result


def get_rows(upload_id: str, row_ids: list[str] | None = None) -> list[dict]:
    """Defensive copies of a session's rows, in upload order.

    If `row_ids` is given, only rows present in BOTH the session and that
    list are returned -- unknown/stale ids are silently dropped rather than
    failing the whole request, since marker removal naturally shrinks the
    set of ids a client sends. Raises SessionNotFound for an unknown/expired
    upload_id (that check always fails closed).
    """
    now = _now()
    with _LOCK:
        session = _get_locked(upload_id, now)
        wanted = set(row_ids) if row_ids is not None else None
        return [
            dict(session['rows'][rid])
            for rid in session['row_order']
            if rid in session['rows'] and (wanted is None or rid in wanted)
        ]


def delete_session(upload_id: str) -> None:
    """Idempotent: deleting an unknown or already-deleted id is not an error."""
    with _LOCK:
        _SESSIONS.pop(upload_id, None)
