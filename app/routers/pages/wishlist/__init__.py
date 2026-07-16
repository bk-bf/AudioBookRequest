from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, BackgroundTasks, Depends, Form, Security
from sqlmodel import Session

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.db_queries import get_wishlist_counts, get_wishlist_results
from app.internal.models import GroupEnum
from app.internal.query import background_auto_download
from app.routers.api.requests import delete_request as api_delete_request
from app.routers.api.requests import mark_downloaded as api_mark_downloaded
from app.routers.api.requests import start_auto_download_endpoint
from app.routers.pages.wishlist.common import render_wishlist
from app.util.toast import ToastException
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.templates import catalog_response

from . import downloaded, manual, sources

router = APIRouter(prefix="/wishlist")

router.include_router(downloaded.router)
router.include_router(manual.router)
router.include_router(sources.router)


@router.get("")
async def wishlist(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    sort_by: str | None = None,
    requested_by: str | None = None,
):
    from app.internal.db_queries import get_requesting_usernames
    from app.internal.prefs import resolve_list_prefs

    sort, req_by = resolve_list_prefs(
        session, user.username, "wishlist", sort_by, requested_by
    )
    username = None if user.is_admin() else user.username
    results = get_wishlist_results(
        session, username, "not_downloaded", sort_by=sort, requested_by=req_by
    )
    counts = get_wishlist_counts(session, user)
    return catalog_response(
        "Wishlist.Index",
        user=user,
        results=results,
        counts=counts,
        sort_by=sort,
        requested_by=req_by,
        usernames=get_requesting_usernames(session),
    )


@router.get("/hx-poll")
async def poll_wishlist(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    page: str = "wishlist",
    sort_by: str | None = None,
    requested_by: str | None = None,
):
    """Polled for progress refreshes and used by the filter bar controls."""
    return render_wishlist(session, user, page, sort_by, requested_by)


@router.post("/hx-bulk/{action}")
async def bulk_action(
    action: str,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    background_task: BackgroundTasks,
    sel_asin: Annotated[list[str] | None, Form()] = None,
    page: str = "wishlist",
):
    """Bulk download/delete/mark for the selected wishlist rows."""
    asins = sel_asin or []
    if not asins:
        raise ToastException("No books selected", "info")
    if action == "download":
        if not user.can_download():
            raise ToastException("Not allowed to download", "error")
        for asin in asins:
            background_task.add_task(background_auto_download, asin)
    elif action == "delete":
        for asin in asins:
            await api_delete_request(asin, session, user)
    elif action == "mark":
        if not user.is_admin():
            raise ToastException("Not allowed", "error")
        for asin in asins:
            await api_mark_downloaded(asin, session, background_task, user)
    else:
        raise ToastException("Unknown bulk action", "error")

    verb = {
        "download": "queued for auto-download",
        "delete": "removed",
        "mark": "marked as downloaded",
    }[action]
    response = render_wishlist(session, user, page)
    response.headers["HX-Trigger"] = "bulkDone"
    _ = verb
    return response


@router.post("/hx-auto-download/{asin}")
async def start_auto_download(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.trusted))],
):
    await start_auto_download_endpoint(asin, session, client_session, user)
    return render_wishlist(session, user, "wishlist")


@router.delete("/hx-delete/{asin}")
async def delete_request(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    downloaded: bool | None = None,
):
    await api_delete_request(asin, session, user)
    return render_wishlist(session, user, "downloaded" if downloaded else "wishlist")
