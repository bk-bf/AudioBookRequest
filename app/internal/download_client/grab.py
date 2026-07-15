import uuid

from aiohttp import ClientSession
from sqlmodel import Session, col, select

from app.internal.download_client.abstract import (
    DownloadClient,
    DownloadClientError,
)
from app.internal.download_client.config import dc_config
from app.internal.download_client.qbittorrent import QbittorrentClient
from app.internal.models import (
    Audiobook,
    DownloadQueueItem,
    DownloadStateEnum,
    ManualBookRequest,
    TorrentSource,
)
from app.util.connection import USER_AGENT
from app.util.log import logger


def get_download_client(session: Session) -> DownloadClient | None:
    """Build the configured download client, or None if not configured/enabled."""
    if not dc_config.is_valid(session):
        return None
    base_url = dc_config.get_base_url(session)
    assert base_url is not None
    return QbittorrentClient(
        base_url=base_url,
        username=dc_config.get_username(session),
        password=dc_config.get_password(session),
    )


def get_active_queue_item(
    session: Session, asin_or_uuid: str
) -> DownloadQueueItem | None:
    """Return the newest non-terminal queue item for a book, if any."""
    try:
        uuid_obj = uuid.UUID(asin_or_uuid)
        clause = col(DownloadQueueItem.manual_request_id) == uuid_obj
    except ValueError:
        clause = col(DownloadQueueItem.asin) == asin_or_uuid
    items = session.exec(
        select(DownloadQueueItem)
        .where(clause)
        .order_by(col(DownloadQueueItem.created_at).desc())
    ).all()
    for item in items:
        if item.state.is_active:
            return item
    return None


async def _fetch_torrent_bytes(
    client_session: ClientSession, download_url: str
) -> bytes | None:
    async with client_session.get(
        download_url, headers={"User-Agent": USER_AGENT}
    ) as r:
        if not r.ok:
            logger.warning(
                "Failed to fetch .torrent, will fall back to magnet",
                download_url=download_url,
                status=r.status,
            )
            return None
        return await r.read()


async def grab_via_client(
    session: Session,
    client_session: ClientSession,
    client: DownloadClient,
    source: TorrentSource,
    book: Audiobook | ManualBookRequest,
) -> DownloadQueueItem:
    """Hand a torrent source directly to ABR's download client and start tracking it.

    Raises DownloadClientError on failure.
    """
    torrent_bytes: bytes | None = None
    if source.download_url:
        torrent_bytes = await _fetch_torrent_bytes(client_session, source.download_url)
    if torrent_bytes is None and not source.magnet_url:
        raise DownloadClientError("Source has no usable download url or magnet link")

    category = dc_config.get_category(session)
    info_hash = await client.add_torrent(source, torrent_bytes, category)

    item = DownloadQueueItem(
        asin=book.asin if isinstance(book, Audiobook) else None,
        manual_request_id=book.id if isinstance(book, ManualBookRequest) else None,
        download_id=info_hash,
        client=dc_config.get_type(session),
        source_title=source.title,
        indexer=source.indexer,
        protocol=source.protocol,
        size=source.size,
        state=DownloadStateEnum.queued,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    logger.info(
        "Download queued via download client",
        info_hash=info_hash,
        title=source.title,
        book=book.title,
    )
    return item
