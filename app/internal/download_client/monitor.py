import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime

import aiohttp
from aiohttp import ClientSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from sqlmodel import Session, col, select

from app.internal.db_queries import get_wishlist_results
from app.internal.download_client.config import dc_config
from app.internal.download_client.grab import (
    get_active_queue_item,
    get_download_client,
)
from app.internal.download_client.importer import import_queue_item
from app.internal.models import DownloadQueueItem, DownloadStateEnum
from app.internal.prowlarr.util import prowlarr_config
from app.internal.ranking.quality import quality_config
from app.util.db import get_session
from app.util.log import logger

POLL_INTERVAL_SECONDS = 10
SWEEP_INTERVAL_MINUTES = 10
LIBRARY_SYNC_INTERVAL_MINUTES = 60
MAX_GRABS_PER_SWEEP = 5
MISSING_GRACE_SECONDS = 300

# in-memory backoff so the sweep doesn't hammer prowlarr for books with no sources
_last_auto_attempt: dict[str, float] = {}


async def _handle_failed_item(session: Session, item: DownloadQueueItem, reason: str):
    """A download failed: clean the torrent + data out of the client and
    retry with the next-best source (failed guids are excluded on retry)."""
    from app.internal.query import background_auto_download

    item.state = DownloadStateEnum.error
    item.error = reason
    session.add(item)
    session.commit()
    client = get_download_client(session)
    if client and item.download_id:
        try:
            await client.remove(item.download_id, delete_files=True)
        except Exception as e:
            logger.warning(
                "Failed to remove failed torrent from client",
                download_id=item.download_id,
                error=str(e),
            )
    logger.info(
        "Download failed, retrying with next-best source",
        title=item.source_title,
        reason=reason,
        asin=item.asin,
    )
    if item.asin:
        asyncio.create_task(background_auto_download(item.asin))
    elif item.manual_request_id:
        asyncio.create_task(background_auto_download(str(item.manual_request_id)))


def _active_items(session: Session) -> list[DownloadQueueItem]:
    return list(
        session.exec(
            select(DownloadQueueItem).where(
                col(DownloadQueueItem.state).in_(
                    [
                        DownloadStateEnum.queued,
                        DownloadStateEnum.downloading,
                        DownloadStateEnum.stalled,
                        DownloadStateEnum.completed,
                    ]
                )
            )
        ).all()
    )


async def poll_download_queue():
    """Sync queue items with the download client; import anything that finished."""
    with next(get_session()) as session:
        if not dc_config.is_valid(session):
            return
        active = _active_items(session)
        if not active:
            return
        client = get_download_client(session)
        if client is None:
            return
        try:
            statuses = {
                s.download_id: s
                for s in await client.list_items(dc_config.get_category(session))
            }
        except Exception as e:
            logger.warning("Download client poll failed", error=str(e))
            return

        async with ClientSession(timeout=aiohttp.ClientTimeout(60)) as client_session:
            for item in active:
                status = statuses.get(item.download_id)
                if status is None:
                    # Give the client a grace period to register the torrent
                    age = (datetime.now() - item.created_at).total_seconds()
                    if age > MISSING_GRACE_SECONDS:
                        item.state = DownloadStateEnum.error
                        item.error = "Download disappeared from the client"
                        session.add(item)
                        logger.warning(
                            "Queue item missing from client",
                            download_id=item.download_id,
                            title=item.source_title,
                        )
                    continue

                item.progress = status.progress
                item.size = status.size or item.size
                item.download_speed = status.download_speed
                item.eta_seconds = status.eta_seconds
                if status.content_path:
                    item.save_path = status.content_path

                if status.state == DownloadStateEnum.error:
                    await _handle_failed_item(
                        session, item, "Download client reports an error"
                    )
                elif status.state == DownloadStateEnum.completed:
                    item.state = DownloadStateEnum.completed
                    session.add(item)
                    session.commit()
                    imported = await import_queue_item(session, client_session, item)
                    if not imported:
                        await _handle_failed_item(
                            session, item, item.error or "Import failed"
                        )
                else:
                    item.state = status.state
                    session.add(item)
            session.commit()


async def auto_download_sweep():
    """Radarr-style wanted sweep: periodically retry auto-download for every
    wishlist book that isn't downloaded and isn't already downloading."""
    # import here to avoid a circular import (query -> prowlarr -> grab)
    from app.internal.query import query_sources

    with next(get_session()) as session:
        if not quality_config.get_auto_download(session):
            return
        try:
            prowlarr_config.raise_if_invalid(session)
        except Exception:
            return

        retry_seconds = dc_config.get_auto_retry_minutes(session) * 60
        results = get_wishlist_results(session, None, "not_downloaded")
        candidates: list[str] = []
        for result in results:
            asin = result.book.asin
            if get_active_queue_item(session, asin):
                continue
            if time.time() - _last_auto_attempt.get(asin, 0) < retry_seconds:
                continue
            candidates.append(asin)

        if not candidates:
            return
        logger.info(
            "Auto-download sweep",
            candidates=len(candidates),
            grabbing=min(len(candidates), MAX_GRABS_PER_SWEEP),
        )
        async with ClientSession(timeout=aiohttp.ClientTimeout(60)) as client_session:
            for asin in candidates[:MAX_GRABS_PER_SWEEP]:
                _last_auto_attempt[asin] = time.time()
                try:
                    await query_sources(
                        asin,
                        session=session,
                        client_session=client_session,
                        start_auto_download=True,
                    )
                except Exception as e:
                    logger.info(
                        "Auto-download attempt failed, will retry later",
                        asin=asin,
                        error=str(e),
                    )


@asynccontextmanager
async def monitor_lifespan(app: FastAPI):
    _ = app
    from app.internal.audiobookshelf.sync import background_sync_abs_library

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        poll_download_queue,
        "interval",
        seconds=POLL_INTERVAL_SECONDS,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        auto_download_sweep,
        "interval",
        minutes=SWEEP_INTERVAL_MINUTES,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        background_sync_abs_library,
        "interval",
        minutes=LIBRARY_SYNC_INTERVAL_MINUTES,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Download queue monitor started")
    # sync the library once at startup so existing books show up immediately
    startup_sync = asyncio.create_task(background_sync_abs_library())
    yield
    _ = startup_sync
    scheduler.shutdown()
