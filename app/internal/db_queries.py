from datetime import datetime
from typing import Literal, Sequence, cast

from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import InstrumentedAttribute, selectinload
from sqlmodel import Session, asc, col, not_, select

from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    AudiobookWishlistResult,
    DownloadQueueItem,
    DownloadStateEnum,
    ManualBookRequest,
    User,
)


def upsert_book_preserving_state(session: Session, book: Audiobook) -> Audiobook:
    """Merge a freshly-fetched (transient) Audiobook into the DB WITHOUT
    clobbering local state. session.merge() copies every field, so a fresh
    Audible result (downloaded=False, missing=False, path=None) would silently
    reset a downloaded or missing book - the root cause of books "vanishing"
    from the library and being re-downloaded."""
    existing = session.get(Audiobook, book.asin)
    if existing:
        book.downloaded = existing.downloaded or book.downloaded
        book.downloaded_path = existing.downloaded_path or book.downloaded_path
        # must come after `downloaded` above: flipping that flag stamps a fresh
        # downloaded_at, which would reset the library position of a book we
        # merely re-fetched metadata for
        book.downloaded_at = existing.downloaded_at or book.downloaded_at
        book.missing = existing.missing
        book.region = book.region or existing.region
        if existing.cover_image and not book.cover_image:
            book.cover_image = existing.cover_image
    return session.merge(book)


class WishlistCounts(BaseModel):
    requests: int
    downloaded: int
    manual: int


def get_wishlist_counts(session: Session, user: User | None = None) -> WishlistCounts:
    """
    If a non-admin user is given, only count requests for that user.
    Admins can see and get counts for all requests.
    """
    username = None if user is None or user.is_admin() else user.username

    # distinct: a book requested by several users is still ONE visible entry
    rows = session.exec(
        select(Audiobook.downloaded, func.count(func.distinct(Audiobook.asin)))
        .where(not username or AudiobookRequest.user_username == username)
        .select_from(Audiobook)
        .join(AudiobookRequest)
        .group_by(col(Audiobook.downloaded))
    ).all()
    requests = 0
    for downloaded_status, count in rows:
        if not downloaded_status:
            requests = count

    # the downloaded tab is a library view: every downloaded book counts,
    # whether it was requested through ABR or synced from Audiobookshelf
    downloaded = session.exec(
        select(func.count()).select_from(Audiobook).where(col(Audiobook.downloaded))
    ).one()

    manual = session.exec(
        select(func.count())
        .select_from(ManualBookRequest)
        .where(
            not username or ManualBookRequest.user_username == username,
            col(ManualBookRequest.user_username).is_not(None),
        )
    ).one()

    return WishlistCounts(
        requests=requests,
        downloaded=downloaded,
        manual=manual,
    )


def _added_at(result: AudiobookWishlistResult) -> datetime:
    """When this entry joined the list the user is looking at: the library
    date for downloaded books, otherwise the date it was first requested.

    Deliberately NOT book.updated_at - that is the audible metadata cache
    marker, re-stamped by every search and refetch, so sorting on it floats
    long-downloaded books back to the top.
    """
    if result.book.downloaded:
        return result.book.downloaded_at or result.book.updated_at
    if result.requests:
        return min(req.updated_at for req in result.requests)
    return result.book.updated_at


def sort_wishlist_results(
    results: list[AudiobookWishlistResult], sort_by: str
) -> list[AudiobookWishlistResult]:
    match sort_by:
        case "title":
            return sorted(results, key=lambda r: r.book.title.lower())
        case "author":
            return sorted(
                results,
                key=lambda r: r.book.authors[0].lower() if r.book.authors else "~",
            )
        case "requester":
            return sorted(
                results,
                key=lambda r: (
                    r.requests[0].user_username.lower() if r.requests else "~"
                ),
            )
        case "downloaded_at":
            return sorted(
                results,
                key=lambda r: r.book.downloaded_at or r.book.updated_at,
                reverse=True,
            )
        case "imported":
            return sorted(
                results,
                key=lambda r: (
                    r.queue.updated_at
                    if r.queue and r.queue.state == DownloadStateEnum.imported
                    else _added_at(r)
                ),
                reverse=True,
            )
        case _:  # "added" - newest first
            return sorted(results, key=_added_at, reverse=True)


def get_wishlist_results(
    session: Session,
    username: str | None = None,
    response_type: Literal["all", "downloaded", "not_downloaded"] = "all",
    sort_by: str = "added",
    requested_by: str = "",
) -> list[AudiobookWishlistResult]:
    """
    Gets the books that have been requested. If a username is given only the books requested by that
    user are returned. If no username is given, all book requests are returned.
    """
    match response_type:
        case "downloaded":
            clause = Audiobook.downloaded
        case "not_downloaded":
            clause = not_(Audiobook.downloaded)
        case _:
            clause = True

    # "downloaded" is a library view (requested or synced from ABS); the
    # other filters stay scoped to requested books
    if response_type == "downloaded":
        request_clause = True
    else:
        request_clause = col(Audiobook.asin).in_(
            select(AudiobookRequest.asin).where(
                not username or AudiobookRequest.user_username == username
            )
        )

    results = session.exec(
        select(Audiobook)
        .where(
            clause,
            request_clause,
        )
        .order_by(asc(Audiobook.title))
        .options(
            selectinload(
                cast(
                    InstrumentedAttribute[list[AudiobookRequest]],
                    cast(object, Audiobook.requests),
                )
            )
        )
    ).all()

    # Attach the newest download-queue item per book so the wishlist can show
    # live download progress and import state.
    queue_map: dict[str, DownloadQueueItem] = {}
    asins = [book.asin for book in results]
    if asins:
        queue_items = session.exec(
            select(DownloadQueueItem)
            .where(col(DownloadQueueItem.asin).in_(asins))
            .order_by(asc(DownloadQueueItem.created_at))
        ).all()
        for item in queue_items:
            if item.asin:
                queue_map[item.asin] = item  # newest wins

    wishlist_results = [
        AudiobookWishlistResult(
            book=book,
            requests=book.requests,
            queue=queue_map.get(book.asin),
        )
        for book in results
    ]
    if requested_by:
        wishlist_results = [
            r
            for r in wishlist_results
            if any(req.user_username == requested_by for req in r.requests)
        ]
    return sort_wishlist_results(wishlist_results, sort_by)


def get_all_manual_requests(
    session: Session,
    user: User,
    sort_by: str = "added",
    requested_by: str = "",
) -> Sequence[ManualBookRequest]:
    results = list(
        session.exec(
            select(ManualBookRequest)
            .where(
                user.is_admin() or ManualBookRequest.user_username == user.username,
                col(ManualBookRequest.user_username).is_not(None),
            )
            .order_by(asc(ManualBookRequest.downloaded))
        ).all()
    )
    if requested_by:
        results = [r for r in results if r.user_username == requested_by]
    match sort_by:
        case "title":
            results.sort(key=lambda r: r.title.lower())
        case "author":
            results.sort(key=lambda r: r.authors[0].lower() if r.authors else "~")
        case "requester":
            results.sort(key=lambda r: r.user_username.lower())
        case _:
            results.sort(key=lambda r: r.updated_at, reverse=True)
    return results


def get_requesting_usernames(session: Session) -> list[str]:
    """Distinct usernames that have requested something (for filter dropdowns)."""
    normal = session.exec(select(AudiobookRequest.user_username).distinct()).all()
    manual = session.exec(select(ManualBookRequest.user_username).distinct()).all()
    return sorted({*normal, *manual})
