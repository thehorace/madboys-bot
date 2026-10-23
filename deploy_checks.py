"""
Which version is running, and a self-check of the bot's settings.

Railway fills in RAILWAY_GIT_* for GitHub deploys, so the bot can say which commit it's on
(handy when you can push to GitHub but can't see Railway). The settings check catches the
mistakes that are easy to make when variables are re-entered by hand.
"""
import os
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

STARTED_AT = time.time()


def running_version() -> dict:
    """{'sha': short commit or '', 'message': first line of the commit message or ''}."""
    return dict(_version())


@lru_cache(maxsize=1)
def _version() -> tuple:
    sha = os.getenv("RAILWAY_GIT_COMMIT_SHA", "").strip()
    message = os.getenv("RAILWAY_GIT_COMMIT_MESSAGE", "").strip()
    if not sha:   # running from a git checkout (e.g. on someone's PC)
        try:
            here = Path(__file__).resolve().parent
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=here, capture_output=True,
                                 text=True, timeout=5).stdout.strip()
            if sha and not message:
                message = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=here, capture_output=True,
                                         text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            sha = ""
    return (("sha", sha[:7]), ("message", message.splitlines()[0][:100] if message else ""))


def version_line() -> str:
    v = running_version()
    if not v["sha"]:
        return f"Unknown version · started <t:{int(STARTED_AT)}:R>"
    msg = f" — {v['message']}" if v["message"] else ""
    return f"`{v['sha']}`{msg}\nStarted <t:{int(STARTED_AT)}:R>"


def _inside(path: str, folder: str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(folder).resolve())
        return True
    except ValueError:
        return False


CHANNEL_VARS = ("MATCHDAY_CHANNEL_ID", "SESSION_CHANNEL_ID", "ANNOUNCE_CHANNEL_ID",
                "MANAGER_CHANNEL_ID", "PATCHNOTES_CHANNEL_ID")


def settings_problems(guild=None, ea=None, db_path: Optional[str] = None) -> list[str]:
    """Plain-words list of setting mistakes; empty when everything looks right."""
    problems = []
    if not os.getenv("MIDDLEWARE_URL", "").strip():
        problems.append("`MIDDLEWARE_URL` isn't set, so stats and match tracking can't reach EA.")
    error = (getattr(ea, "last_error", None) or "") if ea is not None else ""
    ok_since = getattr(ea, "last_ok_at", None) or 0
    if error.startswith(("HTTP 401", "HTTP 403")) and (getattr(ea, "last_error_at", 0) or 0) >= ok_since:
        problems.append("The EA relay is refusing the bot's key. If the keys were changed, set "
                        "`MIDDLEWARE_API_KEY` to the relay's new key.")
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if db_path is None:
        from config import DB_PATH as db_path
    if volume and not _inside(db_path, volume):
        problems.append(f"The database (`DB_PATH` = `{db_path}`) isn't on the Railway volume (`{volume}`), so "
                        f"everything saved is wiped on the next deploy. Set `DB_PATH` to `{volume.rstrip('/')}/"
                        f"{Path(db_path).name}` (or wherever the old database file is on that volume).")
    gid = os.getenv("GUILD_ID", "").strip()
    if gid and not gid.isdigit():
        problems.append("`GUILD_ID` should be just the server's number (no spaces or quotes).")
    for var in CHANNEL_VARS:
        value = os.getenv(var, "").strip()
        if not value:
            continue
        if not value.isdigit():
            problems.append(f"`{var}` should be just the channel's number (no spaces, quotes or #).")
        elif guild is not None and guild.get_channel(int(value)) is None:
            problems.append(f"`{var}` points at a channel the bot can't see ({value}). Fix it or remove it "
                            f"(it's only a default — channels picked with commands still work).")
    return problems
