"""Central registry and task-local club scope; no shared global club switching."""
import functools
import inspect
from contextlib import contextmanager
from contextvars import ContextVar

from config import CLUB_ID, CLUB_NAME
from db import connect, get_setting

_club = ContextVar("club", default=None)


def bound_club_id():
    return _club.get()["club_id"] if _club.get() else None


def match_club(key):
    return int(str(key).split(":", 1)[0]) if ":" in str(key) else CLUB_ID


def match_scoped(fn):
    """Rendering always follows the match, including cards rendered in worker threads."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapped(pm, *args, **kwargs):
            with club_scope(pm.club_id):
                return await fn(pm, *args, **kwargs)
    else:
        @functools.wraps(fn)
        def wrapped(pm, *args, **kwargs):
            with club_scope(pm.club_id):
                return fn(pm, *args, **kwargs)
    return wrapped


def monitored_clubs():
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT club_id,name FROM monitored_clubs WHERE enabled=1 ORDER BY club_id=? DESC,club_id", (CLUB_ID,))]


def club_for(cid):
    with connect() as conn:
        row = conn.execute("SELECT club_id,name FROM monitored_clubs WHERE club_id=?", (int(cid),)).fetchone()
    return dict(row) if row else {"club_id": int(cid), "name": CLUB_NAME if int(cid) == CLUB_ID else f"Club {cid}"}


def club_id():
    return (_club.get() or {"club_id": CLUB_ID})["club_id"]


def club_name():
    return (_club.get() or {"name": CLUB_NAME})["name"]


@contextmanager
def club_scope(club):
    token = _club.set(club if isinstance(club, dict) else club_for(club))
    try:
        yield
    finally:
        _club.reset(token)


def selected_club(gid, uid):
    clubs = monitored_clubs()
    selected = get_setting(str(gid), f"club:{uid}")
    if selected:
        found = next((c for c in clubs if str(c["club_id"]) == selected), None)
        if found:
            return found
    if not clubs:
        return club_for(CLUB_ID)
    # Auto follows recent activity unless this user explicitly picked a club.
    ids = [c["club_id"] for c in clubs]
    with connect() as conn:
        latest = conn.execute(f"SELECT club_id FROM matches WHERE club_id IN ({','.join('?' for _ in ids)}) ORDER BY ts DESC,club_id LIMIT 1", ids).fetchone()
    return next((c for c in clubs if latest and c["club_id"] == latest[0]), clubs[0])


def club_scoped(fn):
    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        if _club.get() is not None:
            return await fn(*args, **kwargs)
        interaction = kwargs.get("interaction") or next((a for a in args if hasattr(a, "guild_id") and hasattr(a, "user")), None)
        if interaction is None:
            return await fn(*args, **kwargs)
        with club_scope(selected_club(interaction.guild_id, interaction.user.id)):
            return await fn(*args, **kwargs)
    return wrapped


def match_key(mid, cid=None):
    """Keep primary-club legacy keys; namespace all secondary-club UI records."""
    cid = cid if cid is not None else club_id()
    return str(mid) if int(cid) == CLUB_ID else f"{cid}:{mid}"


def raw_match_key(key):
    return str(key).split(":", 1)[-1]
