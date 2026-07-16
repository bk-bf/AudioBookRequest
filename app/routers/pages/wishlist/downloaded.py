from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Security
from sqlmodel import Session

from app.internal.audiobookshelf.client import background_abs_trigger_scan
from app.internal.audiobookshelf.config import abs_config
from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.db_queries import get_wishlist_counts, get_wishlist_results
from app.internal.models import GroupEnum
from app.routers.api.requests import mark_downloaded as api_mark_downloaded
from app.routers.pages.wishlist.common import render_wishlist
from app.util.db import get_session
from app.util.templates import catalog_response

router = APIRouter(prefix="/downloaded")


@router.get("")
async def downloaded(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    sort_by: str | None = None,
    requested_by: str | None = None,
):
    from app.internal.db_queries import get_requesting_usernames
    from app.internal.prefs import resolve_list_prefs

    sort, req_by = resolve_list_prefs(
        session, user.username, "downloaded", sort_by, requested_by
    )
    username = None if user.is_admin() else user.username
    results = get_wishlist_results(
        session, username, "downloaded", sort_by=sort, requested_by=req_by
    )
    counts = get_wishlist_counts(session, user)
    return catalog_response(
        "Wishlist.Downloaded",
        user=user,
        results=results,
        counts=counts,
        sort_by=sort,
        requested_by=req_by,
        usernames=get_requesting_usernames(session),
    )


@router.patch("/hx-add/{asin}")
async def update_downloaded(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
):
    await api_mark_downloaded(asin, session, background_task, admin_user)

    if abs_config.is_valid(session):
        background_task.add_task(background_abs_trigger_scan)

    return render_wishlist(session, admin_user, "wishlist")
