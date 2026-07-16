import posixpath
from datetime import datetime

import aiohttp
from aiohttp import ClientSession
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.audiobookshelf.config import abs_config
from app.internal.audiobookshelf.types import ABSBookItemMinified
from app.internal.models import Audiobook
from app.util.connection import USER_AGENT
from app.util.db import get_session
from app.util.log import logger

_PAGE_SIZE = 100


class _ItemsPage(BaseModel):
    results: list[ABSBookItemMinified] = []
    total: int = 0


class SyncResult(BaseModel):
    total_items: int = 0
    matched: int = 0  # items with an ASIN that are now tracked as downloaded
    added: int = 0  # newly created Audiobook rows
    updated: int = 0  # existing rows newly flagged downloaded / repaired


def abs_cover_url(item_id: str) -> str:
    """Browser-reachable cover URL, proxied through ABR (the ABS base url is
    usually an internal docker hostname the client can't resolve)."""
    return f"/abs/cover/{item_id}"


async def _fetch_all_items(
    session: Session, client_session: ClientSession
) -> list[ABSBookItemMinified]:
    base_url = abs_config.get_base_url(session)
    lib_id = abs_config.get_library_id(session)
    token = abs_config.get_api_token(session)
    if not base_url or not lib_id or not token:
        return []
    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    url = posixpath.join(base_url, f"api/libraries/{lib_id}/items")

    items: list[ABSBookItemMinified] = []
    page = 0
    while True:
        params = {"limit": str(_PAGE_SIZE), "page": str(page), "minified": "1"}
        async with client_session.get(url, headers=headers, params=params) as resp:
            if not resp.ok:
                logger.warning(
                    "ABS sync: failed to list items", status=resp.status, page=page
                )
                break
            data = _ItemsPage.model_validate(await resp.json())
        items.extend(data.results)
        if len(items) >= data.total or not data.results:
            break
        page += 1
    return items


def _parse_release_date(published: str | None) -> datetime:
    if published:
        try:
            return datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime(1970, 1, 1)


async def sync_abs_library(
    session: Session, client_session: ClientSession
) -> SyncResult:
    """Full Radarr-style library sync: every ABS library item with an ASIN is
    tracked as a downloaded Audiobook, so the library shows up in ABR."""
    result = SyncResult()
    items = await _fetch_all_items(session, client_session)
    result.total_items = len(items)
    if not items:
        return result

    internal_base = abs_config.get_base_url(session) or ""

    for item in items:
        meta = item.media.metadata
        if not meta.asin:
            continue
        result.matched += 1
        proxy_cover = abs_cover_url(item.id)
        existing = session.get(Audiobook, meta.asin)
        if existing:
            changed = False
            if not existing.downloaded:
                existing.downloaded = True
                changed = True
            if item.path and existing.downloaded_path != item.path:
                existing.downloaded_path = item.path
                changed = True
            # repair covers that point at the internal ABS hostname
            if (
                existing.cover_image
                and internal_base
                and existing.cover_image.startswith(internal_base)
            ):
                existing.cover_image = proxy_cover
                changed = True
            if changed:
                session.add(existing)
                result.updated += 1
        else:
            session.add(
                Audiobook(
                    asin=meta.asin,
                    title=meta.title or "Unknown Title",
                    subtitle=meta.subtitle,
                    authors=[
                        a.strip() for a in meta.authorName.split(",") if a.strip()
                    ],
                    narrators=[
                        n.strip() for n in meta.narratorName.split(",") if n.strip()
                    ],
                    cover_image=proxy_cover,
                    release_date=_parse_release_date(meta.publishedDate),
                    runtime_length_min=int(round(item.media.duration / 60)),
                    downloaded=True,
                    downloaded_path=item.path,
                )
            )
            result.added += 1

    session.commit()
    logger.info(
        "ABS library sync complete",
        total_items=result.total_items,
        matched=result.matched,
        added=result.added,
        updated=result.updated,
    )
    return result


async def background_sync_abs_library():
    with next(get_session()) as session:
        if not abs_config.get_base_url(session) or not abs_config.get_api_token(
            session
        ):
            return
        async with ClientSession(timeout=aiohttp.ClientTimeout(120)) as client_session:
            try:
                await sync_abs_library(session, client_session)
            except Exception as e:
                logger.warning("ABS library sync failed", error=str(e))
