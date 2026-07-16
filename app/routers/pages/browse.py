from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, Query, Security
from sqlmodel import Session

from app.internal.audible.browse import (
    BROWSE_SORTS,
    browse_catalog,
    get_browse_categories,
)
from app.internal.audible.types import get_region_from_settings
from app.internal.audiobookshelf.client import flag_abs_downloaded_items
from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import AudiobookWithRequests
from app.internal.ranking.quality import quality_config
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.templates import catalog_response

router = APIRouter(prefix="/browse")


@router.get("")
async def browse_page(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    category_id: str = "",
    sort: str = "Relevance",
    q: Annotated[str, Query()] = "",
):
    categories = await get_browse_categories(client_session)
    return catalog_response(
        "Browse.Index",
        user=user,
        categories=categories,
        sorts=BROWSE_SORTS,
        category_id=category_id,
        sort=sort,
        keywords=q,
        region=get_region_from_settings(),
        auto_start_download=quality_config.get_auto_download(session),
    )


@router.get("/hx-results")
async def browse_results(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    category_id: str = "",
    sort: str = "Relevance",
    page: int = 0,
    q: Annotated[str, Query()] = "",
):
    page = max(0, page)
    books, total = await browse_catalog(
        client_session,
        category_id=category_id,
        sort=sort,
        page=page,
        keywords=q,
    )
    await flag_abs_downloaded_items(session, client_session, [b.book for b in books])
    results = [
        (
            AudiobookWithRequests(book=b.book, requests=[], username=user.username),
            b.rating_value,
            b.rating_count,
        )
        for b in books
    ]
    return catalog_response(
        "Browse.Results",
        user=user,
        results=results,
        total=total,
        page=page,
        has_next=(page + 1) * 24 < total,
        category_id=category_id,
        sort=sort,
        keywords=q,
        region=get_region_from_settings(),
        auto_start_download=quality_config.get_auto_download(session),
    )
