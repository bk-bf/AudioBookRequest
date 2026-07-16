import uuid
from contextlib import contextmanager
from typing import Literal

import aiohttp
import pydantic
from aiohttp import ClientSession
from fastapi import HTTPException
from sqlmodel import Session

from app.internal.audiobookshelf.client import abs_trigger_scan
from app.internal.audiobookshelf.config import abs_config
from app.internal.download_client.grab import get_active_queue_item
from app.internal.models import (
    Audiobook,
    DownloadQueueItem,
    DownloadStateEnum,
    ManualBookRequest,
    ProwlarrSource,
)
from app.internal.prowlarr.prowlarr import query_prowlarr, start_download
from app.internal.prowlarr.util import prowlarr_config
from app.internal.ranking.download_ranking import rank_sources
from app.util.db import get_session
from app.util.log import logger
from sqlmodel import col, select

querying: set[str] = set()
MAX_AUTO_RETRIES = 3


def get_failed_source_guids(session: Session, asin_or_uuid: str) -> set[str]:
    """Guids of sources that already failed for this book (used to pick the
    next-best source on retry)."""
    try:
        uuid_obj = uuid.UUID(asin_or_uuid)
        clause = col(DownloadQueueItem.manual_request_id) == uuid_obj
    except ValueError:
        clause = col(DownloadQueueItem.asin) == asin_or_uuid
    items = session.exec(
        select(DownloadQueueItem).where(
            clause, col(DownloadQueueItem.state) == DownloadStateEnum.error
        )
    ).all()
    return {i.source_guid for i in items if i.source_guid}


@contextmanager
def manage_queried(asin_or_uuid: str):
    querying.add(asin_or_uuid)
    try:
        yield
    finally:
        try:
            querying.remove(asin_or_uuid)
        except KeyError:
            pass


class QueryResult(pydantic.BaseModel):
    sources: list[ProwlarrSource] | None
    book: Audiobook | ManualBookRequest
    state: Literal["ok", "querying", "uncached"]
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == "ok"


async def query_sources(
    asin_or_uuid: str,
    session: Session,
    client_session: ClientSession,
    force_refresh: bool = False,
    start_auto_download: bool = False,
    only_return_if_cached: bool = False,
) -> QueryResult:
    # First check if the asin_or_uuid is a UUID (manual request)
    try:
        uuid_obj = uuid.UUID(asin_or_uuid)
        book = session.get(ManualBookRequest, uuid_obj)
    except ValueError:
        # Standard Audiobook ASIN
        book = session.get(Audiobook, asin_or_uuid)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    if asin_or_uuid in querying:
        return QueryResult(
            sources=None,
            book=book,
            state="querying",
        )

    with manage_queried(asin_or_uuid):
        prowlarr_config.raise_if_invalid(session)

        sources = await query_prowlarr(
            session,
            client_session,
            book,
            force_refresh=force_refresh,
            only_return_if_cached=only_return_if_cached,
            indexer_ids=prowlarr_config.get_indexers(session),
        )
        if sources is None:
            return QueryResult(
                sources=None,
                book=book,
                state="uncached",
            )

        is_manual = isinstance(book, ManualBookRequest)
        ranked = await rank_sources(session, client_session, sources, book, is_manual)

        # start download if requested
        if start_auto_download and not book.downloaded and len(ranked) > 0:
            # never queue a duplicate while a download for this book is active
            if get_active_queue_item(session, asin_or_uuid):
                logger.info(
                    "Auto-download skipped: already downloading", asin=asin_or_uuid
                )
                return QueryResult(sources=ranked, book=book, state="ok")

            # skip sources that already failed for this book; give up after a few
            failed_guids = get_failed_source_guids(session, asin_or_uuid)
            if len(failed_guids) >= MAX_AUTO_RETRIES:
                raise HTTPException(
                    status_code=500,
                    detail="Too many failed download attempts for this book",
                )
            candidate = next((s for s in ranked if s.guid not in failed_guids), None)
            if candidate is None:
                raise HTTPException(
                    status_code=500,
                    detail="No untried sources left for this book",
                )

            resp = await start_download(
                session=session,
                client_session=client_session,
                guid=candidate.guid,
                indexer_id=candidate.indexer_id,
                asin_or_uuid=asin_or_uuid,
                prowlarr_source=candidate,
            )
            if resp.ok:
                # Try to trigger an ABS scan to pick up new media
                try:
                    if abs_config.is_valid(session):
                        await abs_trigger_scan(session, client_session)
                except Exception:
                    logger.error("Failed to trigger ABS scan after starting download")
            else:
                raise HTTPException(
                    status_code=500,
                    detail=resp.error or "Failed to start download",
                )

        return QueryResult(
            sources=ranked,
            book=book,
            state="ok",
        )


async def background_start_query(asin_or_uuid: str, auto_download: bool):
    with next(get_session()) as session:
        async with ClientSession(timeout=aiohttp.ClientTimeout(60)) as client_session:
            await query_sources(
                asin_or_uuid=asin_or_uuid,
                session=session,
                client_session=client_session,
                start_auto_download=auto_download,
            )


_auto_download_inflight: set[str] = set()


async def background_auto_download(asin_or_uuid: str):
    """Guarded background auto-download: dedupes concurrent attempts per book."""
    if asin_or_uuid in _auto_download_inflight:
        logger.info("Auto-download already in flight", asin=asin_or_uuid)
        return
    _auto_download_inflight.add(asin_or_uuid)
    try:
        with next(get_session()) as session:
            async with ClientSession(
                timeout=aiohttp.ClientTimeout(120)
            ) as client_session:
                await query_sources(
                    asin_or_uuid=asin_or_uuid,
                    session=session,
                    client_session=client_session,
                    start_auto_download=True,
                )
    except Exception as e:
        logger.info("Background auto-download failed", asin=asin_or_uuid, error=str(e))
    finally:
        _auto_download_inflight.discard(asin_or_uuid)
