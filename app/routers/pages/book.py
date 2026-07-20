import uuid
from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Security
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, desc, select
from sqlmodel import delete as sql_delete

from app.internal.audible.extended import get_extended_metadata
from app.internal.audible.similar import list_similar_audible_books
from app.internal.audible.single import get_single_book
from app.internal.audible.types import get_region_from_settings
from app.internal.audiobookshelf.client import (
    abs_get_item_url,
    flag_abs_downloaded_items,
)
from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.download_client.grab import get_active_queue_item, get_download_client
from app.internal.library import (
    delete_imported_files,
    list_library_folders,
    suggest_folders_for_book,
)
from app.internal.query import background_auto_download
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    AudiobookWithRequests,
    DownloadQueueItem,
    DownloadStateEnum,
    GroupEnum,
)
from app.internal.ranking.quality import quality_config
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
            try:
                session.add(book)
                session.commit()
                session.refresh(book)
            except IntegrityError:
                # racing request (link preload + navigation) inserted it first
                session.rollback()
                book = session.get(Audiobook, asin)
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

    extended = await get_extended_metadata(client_session, asin)

    abs_item_url: str | None = None
    if book.downloaded:
        try:
            abs_item_url = await abs_get_item_url(session, client_session, asin)
        except Exception as e:
            logger.debug("ABS item lookup failed", asin=asin, error=str(e))

    from pathlib import Path as _Path

    path_ok: bool | None = None
    path_suggestions: list[str] = []
    library_folders: list[str] = []
    if user.is_admin():
        if book.downloaded_path:
            path_ok = _Path(book.downloaded_path).exists()
        path_suggestions = suggest_folders_for_book(session, book)
        library_folders = list_library_folders(session)

    return catalog_response(
        "Book.Index",
        user=user,
        book=book,
        requests=requests,
        queue_items=queue_items,
        region=get_region_from_settings(),
        extended=extended,
        abs_item_url=abs_item_url,
        active_item=get_active_queue_item(session, asin),
        path_ok=path_ok,
        path_suggestions=path_suggestions,
        library_folders=library_folders,
    )


@router.get("/{asin}/hx-action")
async def book_action(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
):
    """Polled while a download is active to keep the action pill fresh."""
    book, _, _ = await _get_book_context(session, client_session, asin)
    return catalog_response(
        "Book.ActionButton",
        user=user,
        book=book,
        active=get_active_queue_item(session, asin),
    )


@router.get("/{asin}/hx-similar")
async def book_similar(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
):
    """Lazy-loaded 'Similar Titles' shelf on the book detail page."""
    try:
        books = await list_similar_audible_books(
            session, client_session, asin, num_results=10
        )
    except Exception as e:
        logger.warning("Similar titles lookup failed", asin=asin, error=str(e))
        books = []
    books = [b for b in books if b.asin != asin]
    from app.internal.db_queries import upsert_book_preserving_state

    merged = [upsert_book_preserving_state(session, b) for b in books]
    session.commit()
    await flag_abs_downloaded_items(session, client_session, merged)
    results = [
        AudiobookWithRequests(book=b, requests=b.requests, username=user.username)
        for b in merged
    ]
    return catalog_response(
        "Book.Similar",
        results=results,
        user=user,
        region=get_region_from_settings(),
        auto_start_download=quality_config.get_auto_download(session)
        and user.is_above(GroupEnum.trusted),
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
    background_task: BackgroundTasks,
    user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.trusted))],
):
    """Add the book to the wishlist (if needed) and queue an auto-download."""
    book, _, _ = await _get_book_context(session, client_session, asin)
    if book.downloaded:
        raise ToastException("Already downloaded", "info")
    if get_active_queue_item(session, asin):
        raise ToastException("Already downloading", "info")

    if not session.exec(
        select(AudiobookRequest).where(
            AudiobookRequest.asin == asin,
            AudiobookRequest.user_username == user.username,
        )
    ).first():
        session.add(AudiobookRequest(asin=asin, user_username=user.username))
        session.commit()

    background_task.add_task(background_auto_download, asin, True)
    raise ToastException(
        "Added to wishlist — download queued", "success", cause_refresh=True
    )


@router.post("/{asin}/hx-set-status")
async def book_set_status(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    status: Annotated[str, Form()] = "wanted",
):
    """Manually tag a book: wanted / downloaded / missing."""
    _ = admin_user
    book = session.get(Audiobook, asin)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    if status == "downloaded":
        book.downloaded = True
        book.missing = False
    elif status == "missing":
        book.downloaded = False
        book.downloaded_path = None
        book.missing = True
    else:  # wanted
        book.downloaded = False
        book.downloaded_path = None
        book.missing = False
    session.add(book)
    session.commit()
    raise ToastException(f"Marked as {status}", "success", cause_refresh=True)


@router.post("/{asin}/hx-set-path")
async def book_set_path(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    path: Annotated[str, Form()] = "",
):
    """Sonarr-style manual path linking: point a book at files that already
    exist on disk, fix a moved folder, or unlink entirely."""
    _ = admin_user
    book = session.get(Audiobook, asin)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    path = path.split("  (linked to another book)")[0].strip()
    if not path:
        book.downloaded = False
        book.downloaded_path = None
        session.add(book)
        session.commit()
        raise ToastException(
            "Path unlinked — book back to wanted", "success", cause_refresh=True
        )

    from pathlib import Path as _Path

    from app.internal.download_client.config import dc_config

    library_dir = dc_config.get_library_dir(session)
    target = _Path(path).resolve()
    if library_dir:
        lib = _Path(library_dir).resolve()
        if target != lib and lib not in target.parents:
            raise ToastException(
                f"Path must be inside the library ({library_dir})", "error"
            )
    if not target.exists():
        raise ToastException(f"Path does not exist: {target}", "error")
    book.downloaded_path = str(target)
    book.downloaded = True
    book.missing = False
    session.add(book)
    # stale failed download attempts no longer describe reality once the
    # files are linked manually - drop them from the history
    for stale in session.exec(
        select(DownloadQueueItem).where(
            col(DownloadQueueItem.asin) == asin,
            col(DownloadQueueItem.state) == DownloadStateEnum.error,
        )
    ).all():
        session.delete(stale)
    session.commit()
    raise ToastException(
        "Files linked — marked as downloaded", "success", cause_refresh=True
    )


@router.post("/{asin}/hx-toggle-request")
async def book_toggle_request(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
):
    """Add/remove this book on the user's wishlist from the detail page."""
    book, _, _ = await _get_book_context(session, client_session, asin)
    _ = book
    existing = session.exec(
        select(AudiobookRequest).where(
            AudiobookRequest.asin == asin,
            AudiobookRequest.user_username == user.username,
        )
    ).first()
    if existing:
        session.delete(existing)
        session.commit()
        remaining = session.exec(
            select(AudiobookRequest).where(AudiobookRequest.asin == asin)
        ).first()
        if not remaining:
            from app.internal.library import abort_active_downloads

            await abort_active_downloads(session, asin)
        raise ToastException("Removed from wishlist", "success", cause_refresh=True)
    session.add(AudiobookRequest(asin=asin, user_username=user.username))
    session.commit()
    raise ToastException("Added to wishlist", "success", cause_refresh=True)


@router.post("/{asin}/hx-abort/{item_id}")
async def book_abort_download(
    asin: str,
    item_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.trusted))],
):
    """Abort an active download: remove the torrent + data and the queue row."""
    _ = user
    item = session.get(DownloadQueueItem, item_id)
    if not item or item.asin != asin:
        raise HTTPException(status_code=404, detail="Download not found")
    client = get_download_client(session)
    if client and item.download_id:
        try:
            await client.remove(item.download_id, delete_files=True)
        except Exception as e:
            logger.warning(
                "Failed to remove torrent on abort",
                download_id=item.download_id,
                error=str(e),
            )
    session.delete(item)
    session.commit()
    raise ToastException("Download aborted", "success", cause_refresh=True)


@router.post("/{asin}/hx-delete-item/{item_id}")
async def book_delete_queue_item(
    asin: str,
    item_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    remove_torrent: Annotated[bool, Form()] = False,
    remove_request: Annotated[bool, Form()] = False,
):
    """Delete a single download from the book's history: its imported files,
    optionally the torrent + data in the client, and the history row itself."""
    _ = admin_user
    book = session.get(Audiobook, asin)
    item = session.get(DownloadQueueItem, item_id)
    if not book or not item or item.asin != asin:
        raise HTTPException(status_code=404, detail="Download not found")

    deleted = False
    if item.import_path:
        deleted = delete_imported_files(session, item.import_path)

    if remove_torrent and item.download_id:
        client = get_download_client(session)
        if client:
            try:
                await client.remove(item.download_id, delete_files=True)
            except Exception as e:
                logger.warning(
                    "Failed to remove torrent from client",
                    download_id=item.download_id,
                    error=str(e),
                )
    session.delete(item)
    session.commit()

    # Recompute the book's downloaded state from what's left
    remaining = [
        i
        for i in session.exec(
            select(DownloadQueueItem)
            .where(col(DownloadQueueItem.asin) == asin)
            .order_by(col(DownloadQueueItem.created_at).asc())
        ).all()
        if i.state == DownloadStateEnum.imported
    ]
    if remaining:
        book.downloaded_path = remaining[-1].import_path
    else:
        book.downloaded = False
        book.downloaded_path = None
    session.add(book)
    if remove_request:
        session.execute(
            sql_delete(AudiobookRequest).where(col(AudiobookRequest.asin) == asin)
        )
    session.commit()

    raise ToastException(
        "Files deleted" if deleted else "Download removed",
        "success",
        cause_refresh=True,
    )
