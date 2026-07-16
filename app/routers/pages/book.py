import shutil
from pathlib import Path
from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, Form, HTTPException, Security
from sqlmodel import Session, col, delete, desc, select

from app.internal.audible.single import get_single_book
from app.internal.audible.types import get_region_from_settings
from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.download_client.config import dc_config
from app.internal.download_client.grab import get_download_client
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    DownloadQueueItem,
    GroupEnum,
)
from app.routers.api.requests import start_auto_download_endpoint
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.log import logger
from app.util.templates import catalog_response
from app.util.toast import ToastException

router = APIRouter(prefix="/book")


async def _get_book_context(session: Session, client_session: ClientSession, asin: str):
    book = session.get(Audiobook, asin)
    if not book:
        # Search results aren't stored in the Audiobook table until requested;
        # fetch from Audible on demand so any book card can link here.
        try:
            book = await get_single_book(client_session, asin=asin)
        except Exception as e:
            logger.warning("Failed to fetch book from Audible", asin=asin, error=str(e))
        if book:
            session.add(book)
            session.commit()
            session.refresh(book)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    requests = session.exec(
        select(AudiobookRequest).where(AudiobookRequest.asin == asin)
    ).all()
    queue_items = session.exec(
        select(DownloadQueueItem)
        .where(col(DownloadQueueItem.asin) == asin)
        .order_by(desc(DownloadQueueItem.created_at))
    ).all()
    return book, list(requests), list(queue_items)


@router.get("/{asin}")
async def book_detail(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
):
    book, requests, queue_items = await _get_book_context(session, client_session, asin)
    return catalog_response(
        "Book.Index",
        user=user,
        book=book,
        requests=requests,
        queue_items=queue_items,
        region=get_region_from_settings(),
    )


@router.get("/{asin}/hx-status")
async def book_status(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
):
    """Polled by the detail page while a download is active."""
    book, _, queue_items = await _get_book_context(session, client_session, asin)
    return catalog_response(
        "Book.Status",
        user=user,
        book=book,
        queue_items=queue_items,
    )


@router.post("/{asin}/hx-auto-download")
async def book_auto_download(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.trusted))],
):
    await start_auto_download_endpoint(asin, session, client_session, user)
    raise ToastException("Download started", "success", cause_refresh=True)


def _delete_imported_files(session: Session, downloaded_path: str) -> bool:
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


@router.post("/{asin}/hx-delete-files")
async def book_delete_files(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    remove_torrent: Annotated[bool, Form()] = False,
    remove_request: Annotated[bool, Form()] = False,
):
    """Radarr-style 'delete files': remove the imported files from the library,
    reset the book to wanted, and optionally drop the torrent + wishlist request."""
    _ = admin_user
    book = session.get(Audiobook, asin)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    deleted = False
    if book.downloaded_path:
        deleted = _delete_imported_files(session, book.downloaded_path)

    queue_items = session.exec(
        select(DownloadQueueItem).where(col(DownloadQueueItem.asin) == asin)
    ).all()
    client = get_download_client(session)
    for item in queue_items:
        if remove_torrent and client and item.download_id:
            try:
                await client.remove(item.download_id, delete_files=True)
            except Exception as e:
                logger.warning(
                    "Failed to remove torrent from client",
                    download_id=item.download_id,
                    error=str(e),
                )
        session.delete(item)

    book.downloaded = False
    book.downloaded_path = None
    session.add(book)
    if remove_request:
        session.execute(
            delete(AudiobookRequest).where(col(AudiobookRequest.asin) == asin)
        )
    session.commit()

    parts = [
        "Files deleted" if deleted else "No files on disk, state reset",
    ]
    if remove_torrent and queue_items:
        parts.append("torrent removed")
    if remove_request:
        parts.append("request removed")
    raise ToastException(", ".join(parts), "success", cause_refresh=True)
