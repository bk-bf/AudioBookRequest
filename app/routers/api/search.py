from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, HTTPException, Query, Security
from sqlmodel import Session

from app.internal.audible.search import get_search_suggestions, search_audible_books
from app.internal.audible.types import (
    audible_region_type,
    audible_regions,
    get_region_from_settings,
)
from app.internal.audiobookshelf.client import abs_mark_downloaded_flags
from app.internal.auth.authentication import AnyAuth, DetailedUser
from app.internal.models import Audiobook, AudiobookWithRequests
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.log import logger

router = APIRouter(prefix="/search", tags=["Search"])


@router.get("", response_model=list[AudiobookWithRequests])
async def search_books(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(AnyAuth())],
    query: Annotated[str | None, Query(alias="q")] = None,
    num_results: int = 20,
    page: int = 0,
    region: audible_region_type | None = None,
):
    if region is None:
        region = get_region_from_settings()
    if audible_regions.get(region) is None:
        raise HTTPException(status_code=400, detail="Invalid region")
    if query:
        results = await search_audible_books(
            client_session=client_session,
            query=query,
            num_results=num_results,
            page=page,
            audible_region=region,
        )
    else:
        results = []

    # refreshes the "requests"
    merged: list[Audiobook] = []
    for res in results:
        # session.merge overwrites ALL fields; don't let a fresh Audible result
        # clobber the downloaded state or imported path of a known book
        existing = session.get(Audiobook, res.asin)
        if existing:
            res.downloaded = existing.downloaded or res.downloaded
            res.downloaded_path = existing.downloaded_path
            if existing.cover_image and not res.cover_image:
                res.cover_image = existing.cover_image
        merged.append(session.merge(res))

    # flag books that already exist in the Audiobookshelf library
    try:
        await abs_mark_downloaded_flags(session, client_session, merged)
    except Exception as e:
        logger.warning("ABS downloaded-check failed during search", error=str(e))

    return [
        AudiobookWithRequests(
            book=book,
            requests=book.requests,
            username=user.username,
        )
        for book in merged
    ]


@router.get("/suggestions", response_model=list[str])
async def search_suggestions(
    query: Annotated[str, Query(alias="q")],
    _: Annotated[DetailedUser, Security(AnyAuth())],
    region: audible_region_type | None = None,
):
    if region is None:
        region = get_region_from_settings()
    async with ClientSession() as client_session:
        return await get_search_suggestions(client_session, query, region)
