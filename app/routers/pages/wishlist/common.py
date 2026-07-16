from sqlmodel import Session

from app.internal.auth.authentication import DetailedUser
from app.internal.db_queries import (
    get_all_manual_requests,
    get_requesting_usernames,
    get_wishlist_counts,
    get_wishlist_results,
)
from app.internal.prefs import resolve_list_prefs
from app.util.templates import catalog_response


def render_wishlist(
    session: Session,
    user: DetailedUser,
    page: str,
    sort_by: str | None = None,
    requested_by: str | None = None,
    update_tablist: bool = True,
):
    """Render the requests/downloaded list with the user's saved (or newly
    chosen, then saved) sort/filter preferences for that tab."""
    pref_page = "downloaded" if page == "downloaded" else "wishlist"
    sort, req_by = resolve_list_prefs(
        session, user.username, pref_page, sort_by, requested_by
    )
    username = None if user.is_admin() else user.username
    results = get_wishlist_results(
        session,
        username,
        "downloaded" if page == "downloaded" else "not_downloaded",
        sort_by=sort,
        requested_by=req_by,
    )
    counts = get_wishlist_counts(session, user)
    return catalog_response(
        "Wishlist.Wishlist",
        user=user,
        results=results,
        page=page,
        counts=counts,
        update_tablist=update_tablist,
        sort_by=sort,
        requested_by=req_by,
        usernames=get_requesting_usernames(session),
    )


def render_manual_wishlist(
    session: Session,
    user: DetailedUser,
    sort_by: str | None = None,
    requested_by: str | None = None,
    update_tablist: bool = True,
):
    sort, req_by = resolve_list_prefs(
        session, user.username, "manual", sort_by, requested_by
    )
    results = get_all_manual_requests(session, user, sort_by=sort, requested_by=req_by)
    counts = get_wishlist_counts(session, user)
    return catalog_response(
        "Wishlist.ManualWishlist",
        user=user,
        results=results,
        counts=counts,
        update_tablist=update_tablist,
        sort_by=sort,
        requested_by=req_by,
        usernames=get_requesting_usernames(session),
    )
