import json
from typing import Literal

from sqlmodel import Session, select

from app.internal.models import Config
from app.util.log import logger

WishlistPage = Literal["wishlist", "downloaded", "manual"]

SORT_VALUES = ("added", "title", "author", "requester")

PrefDict = dict[str, dict[str, str]]


def _key(username: str) -> str:
    return f"user_prefs:{username}"


def get_user_prefs(session: Session, username: str) -> PrefDict:
    row = session.exec(select(Config).where(Config.key == _key(username))).one_or_none()
    if not row:
        return {}
    try:
        parsed: object = json.loads(row.value)  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        logger.warning("Corrupt user prefs, resetting", username=username)
        return {}
    if not isinstance(parsed, dict):
        return {}
    prefs: PrefDict = {}
    for page, values in parsed.items():  # pyright: ignore[reportUnknownVariableType]
        if isinstance(page, str) and isinstance(values, dict):
            prefs[page] = {
                str(k): str(v)
                for k, v in values.items()  # pyright: ignore[reportUnknownVariableType]
                if isinstance(k, str) and isinstance(v, str)
            }
    return prefs


def set_user_prefs(session: Session, username: str, prefs: PrefDict) -> None:
    row = session.exec(select(Config).where(Config.key == _key(username))).one_or_none()
    value = json.dumps(prefs)
    if row:
        row.value = value
    else:
        row = Config(key=_key(username), value=value)
    session.add(row)
    session.commit()


def resolve_list_prefs(
    session: Session,
    username: str,
    page: WishlistPage,
    sort_by: str | None,
    requested_by: str | None,
) -> tuple[str, str]:
    """Per-user, per-tab list preferences. Explicit query params update the
    stored preference; absent params fall back to what the user saved."""
    prefs = get_user_prefs(session, username)
    page_prefs: dict[str, str] = prefs.get(page, {})

    if sort_by is not None or requested_by is not None:
        new_page_prefs = {
            "sort_by": sort_by if sort_by in SORT_VALUES else "added",
            "requested_by": requested_by or "",
        }
        if new_page_prefs != page_prefs:
            prefs[page] = new_page_prefs
            set_user_prefs(session, username, prefs)
        page_prefs = new_page_prefs

    sort = page_prefs.get("sort_by", "added")
    return (
        sort if sort in SORT_VALUES else "added",
        page_prefs.get("requested_by", ""),
    )
