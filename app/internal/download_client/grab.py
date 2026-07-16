import uuid
from urllib.parse import urljoin

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


async def resolve_source_payload(
    client_session: ClientSession, source: TorrentSource
) -> tuple[bytes | None, str | None]:
    """Resolve a source's urls to something a client can consume:
    (torrent file bytes, magnet link) — at least one is set on success.

    Prowlarr proxies both .torrent files AND magnet links behind its own
    http download endpoint (a magnet result answers with a redirect to the
    magnet: URI), so redirects have to be followed manually.
    """
    urls = [u for u in (source.download_url, source.magnet_url) if u]
    for url in urls:
        if url.startswith("magnet:"):
            return None, url

    for url in urls:
        current = url
        for _ in range(5):  # redirect hop limit
            try:
                async with client_session.get(
                    current,
                    headers={"User-Agent": USER_AGENT},
                    allow_redirects=False,
                ) as r:
                    if r.status in (301, 302, 303, 307, 308):
                        location = r.headers.get("Location", "")
                        if location.startswith("magnet:"):
                            return None, location
                        if not location:
                            break
                        current = urljoin(current, location)
                        continue
                    if r.ok:
                        content = await r.read()
                        # bencoded torrent files always start with a dict
                        if content[:1] == b"d":
                            return content, None
                        logger.warning(
                            "Download url returned non-torrent content",
                            url=current,
                            content_type=r.headers.get("Content-Type"),
                        )
                    else:
                        logger.warning(
                            "Failed to fetch source url",
                            url=current,
                            status=r.status,
                        )
                    break
            except Exception as e:
                logger.warning("Error fetching source url", url=current, error=str(e))
                break
    return None, None


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
    torrent_bytes, magnet_url = await resolve_source_payload(client_session, source)
    if torrent_bytes is None and not magnet_url:
        raise DownloadClientError("Source has no usable download url or magnet link")
    if magnet_url and magnet_url != source.magnet_url:
        source = source.model_copy(update={"magnet_url": magnet_url})

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
