import asyncio
import random
from datetime import date
from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, Query, Security
from pydantic import BaseModel
from sqlalchemy.sql.functions import count
from sqlmodel import Session, col, desc, select

from app.internal.audible.extended import get_extended_metadata
from app.internal.audible.types import audible_region_type, get_region_from_settings
from app.internal.audiobookshelf.client import flag_abs_downloaded_items
from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import Audiobook, AudiobookRequest, AudiobookWithRequests
from app.internal.ranking.quality import quality_config
from app.routers.api.recommendations import (
    get_category_recommendations as api_get_category_recommendations,
)
from app.routers.api.recommendations import (
    get_fallback_recommendations as api_get_fallback_recommendations,
)
from app.routers.api.recommendations import (
    get_popular_authors_recommendations as api_get_popular_authors_recommendations,
)
from app.routers.api.recommendations import (
    get_popular_narrators_recommendations as api_get_popular_narrators_recommendations,
)
from app.routers.api.recommendations import (
    get_popular_recommendations as api_get_popular_recommendations,
)
from app.routers.api.recommendations import (
    get_recently_requested_recommendations as api_get_recently_requested_recommendations,
)
from app.routers.api.recommendations import (
    get_user_recommendations as api_get_user_recommendations,
)
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.templates import catalog_response

router = APIRouter()


def _dedupe_books(
    items: list[AudiobookWithRequests],
) -> list[AudiobookWithRequests]:
    """Collapse the same title+author appearing from multiple regions/editions."""
    seen: set[tuple[str, str]] = set()
    out: list[AudiobookWithRequests] = []
    for item in items:
        key = (
            item.book.title.strip().lower(),
            item.book.authors[0].strip().lower() if item.book.authors else "",
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


@router.get("/")
def read_root(
    user: Annotated[DetailedUser, Security(ABRAuth())],
    session: Annotated[Session, Depends(get_session)],
):
    # no need to show the popular tab if there are no requests from other users
    show_popular = (
        0
        < session.exec(
            select(count("*")).where(AudiobookRequest.user_username != user.username)
        ).one()
    )
    return catalog_response(
        "Index.Index",
        user=user,
        region=get_region_from_settings(),
        auto_download=quality_config.get_auto_download(session),
        show_popular=show_popular,
    )


@router.get("/hx-hero")
async def get_hero(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
):
    """Featured book of the day, from Audible's popular list."""
    try:
        books = await api_get_fallback_recommendations(
            session=session,
            client_session=client_session,
            user=user,
            limit=12,
            audible_region=None,
        )
    except Exception:
        books = []
    books = _dedupe_books(books)
    if not books:
        return catalog_response("Index.Empty")
    await flag_abs_downloaded_items(session, client_session, books)
    # rotate the slide order daily so the first slide changes every day
    offset = date.today().toordinal() % len(books)
    slides = (books[offset:] + books[:offset])[:6]
    extended_list = await asyncio.gather(
        *[get_extended_metadata(client_session, s.book.asin) for s in slides]
    )
    return catalog_response(
        "Index.Hero",
        slides=list(zip(slides, extended_list)),
        user=user,
        region=get_region_from_settings(),
        auto_start_download=quality_config.get_auto_download(session),
    )


@router.get("/hx-recent-library")
async def get_recently_added(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    limit: int = 12,
):
    """Latest additions to the library."""
    books = session.exec(
        select(Audiobook)
        .where(col(Audiobook.downloaded))
        .order_by(desc(Audiobook.updated_at))
        .limit(limit)
    ).all()
    reasons = [
        _AudiobookReasonWrapper(
            book=AudiobookWithRequests(
                book=b, requests=b.requests, username=user.username
            ),
            reason="Recently added",
        )
        for b in books
    ]
    return catalog_response(
        "Index.PopularSection",
        title="Recently Added",
        reasons=reasons,
        user=user,
        description="Latest additions to your library",
        view_more="/wishlist/downloaded",
        region=get_region_from_settings(),
        auto_start_download=quality_config.get_auto_download(session),
    )


@router.get("/hx-for-you")
async def get_user_recommendations(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    seed_asins: Annotated[list[str] | None, Query(alias="seed_asins")] = None,
    limit: int = 20,
):
    result = await api_get_user_recommendations(
        session=session,
        client_session=client_session,
        user=user,
        seed_asins=seed_asins,
        limit=limit,
    )

    await flag_abs_downloaded_items(session, client_session, result.recommendations)

    return catalog_response(
        "Index.PopularSection",
        title="For You",
        user=user,
        reasons=result.recommendations,
        view_more="/recommendations/for-you",
        description="Personalized recommendations based on your requests",
        empty="No recommendations available at this time. Request some books to start getting recommendations.",
        region=get_region_from_settings(),
        auto_start_download=quality_config.get_auto_download(session),
    )


@router.get("/hx-popular")
async def get_popular_recommendations(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    min_requests: int = 1,
    limit: int = 10,
    exclude_downloaded: bool = True,
):
    result = await api_get_popular_recommendations(
        session=session,
        user=user,
        min_requests=min_requests,
        limit=limit,
        exclude_downloaded=exclude_downloaded,
    )

    await flag_abs_downloaded_items(session, client_session, result)

    return catalog_response(
        "Index.PopularSection",
        title="Popular on this server",
        user=user,
        reasons=result,
        description="Most requested by users on this server",
        empty="No popular recommendations available at this time. Request some books to start getting recommendations.",
        region=get_region_from_settings(),
        auto_start_download=quality_config.get_auto_download(session),
    )


@router.get("/hx-categories")
async def get_category_recommendations(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    audible_region: audible_region_type | None = None,
):
    result = await api_get_category_recommendations(
        session=session,
        client_session=client_session,
        user=user,
        audible_region=audible_region,
    )

    rng = random.Random(date.today().toordinal())
    result = {k: rng.sample(v, len(v)) for k, v in result.items()}

    all_category_books = [b for books in result.values() for b in books]
    await flag_abs_downloaded_items(session, client_session, all_category_books)

    region = audible_region or get_region_from_settings()

    return catalog_response(
        "Index.Categories",
        categories=result,
        region=region,
        auto_start_download=quality_config.get_auto_download(session),
        user=user,
    )


"""

UNUSED ENDPOINTS


"""


class _AudiobookReasonWrapper(BaseModel):
    book: AudiobookWithRequests
    reason: str


@router.get("/hx-recent")
async def get_recently_requested_recommendations(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    limit: int = 10,
    days_back: int = 30,
    exclude_downloaded: bool = True,
):
    result = await api_get_recently_requested_recommendations(
        session=session,
        user=user,
        limit=limit,
        days_back=days_back,
        exclude_downloaded=exclude_downloaded,
    )

    reasons = [
        _AudiobookReasonWrapper(book=book, reason="Recently requested")
        for book in result
    ]

    return catalog_response(
        "Index.PopularSection",
        reasons=reasons,
        user=user,
        description="Books that have been recently requested by users on the instance",
        empty="No recently requested recommendations available at this time. Request some books to start getting recommendations.",
        region=get_region_from_settings(),
        auto_start_download=quality_config.get_auto_download(session),
    )


@router.get("/hx-fallback")
async def get_fallback_recommendations(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    limit: int = 10,
    audible_region: audible_region_type | None = None,
):
    """Get fallback popular recommendations from Audible search. Does not take into account user history or preference."""

    result = await api_get_fallback_recommendations(
        session=session,
        client_session=client_session,
        user=user,
        limit=limit,
        audible_region=audible_region,
    )

    reasons = [
        _AudiobookReasonWrapper(book=book, reason="Popular on Audible")
        for book in _dedupe_books(result)
    ]

    region = audible_region or get_region_from_settings()

    return catalog_response(
        "Index.PopularSection",
        title="Popular on Audible",
        reasons=reasons,
        user=user,
        description="What everyone is listening to right now",
        empty="No fallback recommendations available at this time.",
        region=region,
        auto_start_download=quality_config.get_auto_download(session),
    )


@router.get("/hx-authors")
async def get_popular_authors_recommendations(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    limit: int = 10,
    exclude_downloaded: bool = True,
    audible_region: audible_region_type | None = None,
    personal_favorites: bool = True,
):
    result = await api_get_popular_authors_recommendations(
        session=session,
        client_session=client_session,
        user=user,
        limit=limit,
        exclude_downloaded=exclude_downloaded,
        audible_region=audible_region,
        personal_favorites=personal_favorites,
    )

    reasons = [
        _AudiobookReasonWrapper(book=book, reason="Popular author") for book in result
    ]

    region = audible_region or get_region_from_settings()

    return catalog_response(
        "Index.PopularSection",
        reasons=reasons,
        user=user,
        description="Books from popular authors",
        empty="No popular author recommendations available at this time. Request some books to start getting recommendations.",
        region=region,
        auto_start_download=quality_config.get_auto_download(session),
    )


@router.get("/hx-narrators")
async def get_popular_narrators_recommendations(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
    limit: int = 10,
    exclude_downloaded: bool = True,
    audible_region: audible_region_type | None = None,
    personal_favorites: bool = True,
):
    result = await api_get_popular_narrators_recommendations(
        session=session,
        client_session=client_session,
        user=user,
        limit=limit,
        exclude_downloaded=exclude_downloaded,
        audible_region=audible_region,
        personal_favorites=personal_favorites,
    )

    reasons = [
        _AudiobookReasonWrapper(book=book, reason="Popular narrator") for book in result
    ]

    region = audible_region or get_region_from_settings()

    return catalog_response(
        "Index.PopularSection",
        reasons=reasons,
        user=user,
        description="Books from popular narrators",
        empty="No popular narrator recommendations available at this time. Request some books to start getting recommendations.",
        region=region,
        auto_start_download=quality_config.get_auto_download(session),
    )
