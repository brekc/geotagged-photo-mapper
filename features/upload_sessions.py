"""Store bounded, process-local upload sessions in memory."""

import secrets
import threading
import time

_LOCK = threading.Lock()
# Process-local: rows live only in this worker's memory, never photo bytes or
# filesystem paths. Multi-worker/multi-process deployments need a shared
# external store, or requests may miss sessions created by another worker.
_SESSIONS: dict[str, dict] = {}

SESSION_TTL_SECONDS = 15 * 60
MAX_SESSIONS = 200
MAX_ROWS_PER_SESSION = 2000
MAX_TOTAL_ROWS = 20000


class SessionError(Exception):
    # Base class for session failures. Every caller must fail closed on these.
    pass


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
    # Evict the least-recently-accessed session instead of exceeding the cap.
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


# Create an empty session and return its opaque upload_id.
def create_session() -> str:
    upload_id = secrets.token_urlsafe(32)
    now = _now()
    with _LOCK:
        _prune_expired_locked(now)
        while len(_SESSIONS) >= MAX_SESSIONS:
            _evict_oldest_locked()
        _SESSIONS[upload_id] = {
            'created_at': now,
            'last_access': now,
            'rows': {},       # photo_id -> normalized feature dict
            'row_order': [],  # photo_ids in upload order, for SequenceOrder
        }
    return upload_id


# Replace a session's rows and return defensive copies with new `photo_id`
# values. Unknown sessions and row-limit violations fail explicitly.
def set_rows(upload_id: str, features: list[dict]) -> list[dict]:
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
            photo_id = secrets.token_urlsafe(16)
            row = dict(feature)
            row['photo_id'] = photo_id
            rows[photo_id] = row
            row_order.append(photo_id)
            result.append(dict(row))

        session['rows'] = rows
        session['row_order'] = row_order
        return result


# Return defensive row copies in upload order. When `photo_ids` is supplied,
# silently omit stale IDs because marker removal intentionally shrinks the
# selection; an unknown or expired upload_id still fails closed.
def get_rows(upload_id: str, photo_ids: list[str] | None = None) -> list[dict]:
    now = _now()
    with _LOCK:
        session = _get_locked(upload_id, now)
        wanted = set(photo_ids) if photo_ids is not None else None
        return [
            dict(session['rows'][pid])
            for pid in session['row_order']
            if pid in session['rows'] and (wanted is None or pid in wanted)
        ]


# Idempotent: deleting an unknown or already-deleted id is not an error.
def delete_session(upload_id: str) -> None:
    with _LOCK:
        _SESSIONS.pop(upload_id, None)
