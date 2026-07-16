import shutil
from pathlib import Path

from sqlmodel import Session, col, delete, select

from app.internal.download_client.config import dc_config
from app.internal.download_client.grab import get_download_client
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    DownloadQueueItem,
)
from app.util.log import logger


def delete_imported_files(session: Session, downloaded_path: str) -> bool:
    """Delete an imported book directory, but only if it sits strictly inside
    the configured library directory. Returns True if files were removed."""
    library_dir = dc_config.get_library_dir(session)
    if not library_dir:
        return False
    lib = Path(library_dir).resolve()
    target = Path(downloaded_path).resolve()
    if target == lib or lib not in target.parents:
        logger.warning("Refusing to delete path outside the library", path=str(target))
        return False
    if not target.exists():
        return False
    shutil.rmtree(target)
    try:
        target.parent.rmdir()  # clean up the author dir if now empty
    except OSError:
        pass
    logger.info("Deleted imported files", path=str(target))
    return True


async def abort_active_downloads(session: Session, asin: str) -> int:
    """Remove any non-imported downloads of a book from the client (with
    data) and drop their queue rows. Returns how many were aborted."""
    items = session.exec(
        select(DownloadQueueItem).where(col(DownloadQueueItem.asin) == asin)
    ).all()
    active = [i for i in items if i.state.is_active]
    if not active:
        return 0
    client = get_download_client(session)
    for item in active:
        if client and item.download_id:
            try:
                await client.remove(item.download_id, delete_files=True)
            except Exception as e:
                logger.warning(
                    "Failed to remove torrent while aborting",
                    download_id=item.download_id,
                    error=str(e),
                )
        session.delete(item)
    session.commit()
    logger.info("Aborted active downloads", asin=asin, count=len(active))
    return len(active)


async def delete_book_media(
    session: Session,
    asin: str,
    remove_torrent: bool = True,
    remove_requests: bool = True,
) -> bool:
    """Fully remove a book's media: imported files (restricted to the library
    dir), torrents + data in the client, download history, and optionally the
    wishlist requests so auto-download won't immediately re-grab it."""
    book = session.get(Audiobook, asin)
    if not book:
        return False

    deleted = False
    items = session.exec(
        select(DownloadQueueItem).where(col(DownloadQueueItem.asin) == asin)
    ).all()
    client = get_download_client(session) if remove_torrent else None
    for item in items:
        if client and item.download_id:
            try:
                await client.remove(item.download_id, delete_files=True)
            except Exception as e:
                logger.warning(
                    "Failed to remove torrent from client",
                    download_id=item.download_id,
                    error=str(e),
                )
        if item.import_path:
            deleted = delete_imported_files(session, item.import_path) or deleted
        session.delete(item)

    if book.downloaded_path:
        deleted = delete_imported_files(session, book.downloaded_path) or deleted

    book.downloaded = False
    book.downloaded_path = None
    session.add(book)
    if remove_requests:
        session.execute(
            delete(AudiobookRequest).where(col(AudiobookRequest.asin) == asin)
        )
    session.commit()
    return deleted
