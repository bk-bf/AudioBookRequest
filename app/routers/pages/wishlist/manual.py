import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Security
from sqlmodel import Session

from app.internal.audiobookshelf.client import background_abs_trigger_scan
from app.internal.audiobookshelf.config import abs_config
from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.db_queries import get_all_manual_requests, get_wishlist_counts
from app.internal.models import GroupEnum
from app.routers.api.requests import delete_manual_request, mark_manual_downloaded
from app.routers.pages.wishlist.common import render_manual_wishlist
from app.util.db import get_session
from app.util.templates import catalog_response

router = APIRouter(prefix="/manual")


@router.get("")
async def manual(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    sort_by: str | None = None,
    requested_by: str | None = None,
):
    from app.internal.db_queries import get_requesting_usernames
    from app.internal.prefs import resolve_list_prefs

    sort, req_by = resolve_list_prefs(
        session, user.username, "manual", sort_by, requested_by
    )
    results = get_all_manual_requests(session, user, sort_by=sort, requested_by=req_by)
    counts = get_wishlist_counts(session, user)
    return catalog_response(
        "Wishlist.Manual",
        user=user,
        results=results,
        counts=counts,
        sort_by=sort,
        requested_by=req_by,
        usernames=get_requesting_usernames(session),
    )


@router.get("/hx-list")
async def manual_list(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    sort_by: str | None = None,
    requested_by: str | None = None,
):
    return render_manual_wishlist(session, user, sort_by, requested_by)


@router.patch("/hx-add/{id}")
async def downloaded_manual(
    id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
):
    await mark_manual_downloaded(id, session, background_task, admin_user)

    if abs_config.is_valid(session):
        background_task.add_task(background_abs_trigger_scan)

    return render_manual_wishlist(session, admin_user)


@router.delete("/hx-delete/{id}")
async def delete_manual(
    id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
):
    await delete_manual_request(id, session, admin_user)
    return render_manual_wishlist(session, admin_user)
