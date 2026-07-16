import asyncio

from aiohttp import ClientSession
from pydantic import BaseModel

from app.internal.audible.types import (
    AudibleProduct,
    audible_region_type,
    audible_regions,
    get_region_from_settings,
)
from app.internal.models import Audiobook
from app.util.cache import SimpleCache
from app.util.log import logger

_CATEGORY_TTL_SECONDS = 60 * 60 * 24 * 7
_PAGE_SIZE = 24

# valid unauthenticated products_sort_by values (verified against the API)
BROWSE_SORTS: list[tuple[str, str]] = [
    ("Relevance", "Relevance"),
    ("AvgRating", "Top rated"),
    ("-ReleaseDate", "New releases"),
    ("-RuntimeLength", "Longest"),
    ("RuntimeLength", "Shortest"),
]
_VALID_SORTS = {value for value, _ in BROWSE_SORTS}

# keyword samples used to harvest category ids from category_ladders
# (the /1.0/categories endpoint requires authentication)
_SEED_KEYWORDS = [
    "bestseller",
    "fantasy",
    "science fiction",
    "romance",
    "thriller",
    "business",
    "history",
    "self help",
    "biography",
    "comedy",
    "kids",
    "true crime",
]


class BrowseCategory(BaseModel):
    id: str
    name: str


class _RatedProduct(AudibleProduct):
    class _Rating(BaseModel):
        class _Distribution(BaseModel):
            display_average_rating: float | None = None
            num_ratings: int | None = None

        overall_distribution: _Distribution = _Distribution()

    rating: _Rating | None = None


class _CatalogPage(BaseModel):
    products: list[_RatedProduct] = []
    total_results: int = 0


class _LadderEntry(BaseModel):
    id: str
    name: str


class _LadderProduct(BaseModel):
    class _Ladder(BaseModel):
        ladder: list[_LadderEntry] = []

    category_ladders: list[_Ladder] = []


class _LadderPage(BaseModel):
    products: list[_LadderProduct] = []


class BrowseBook(BaseModel):
    book: Audiobook
    rating_value: float | None = None
    rating_count: int | None = None


_categories_cache: SimpleCache[list[BrowseCategory], str] = SimpleCache()


async def get_browse_categories(
    client_session: ClientSession,
    region: audible_region_type | None = None,
) -> list[BrowseCategory]:
    """Harvest browsable category ids from search results' category ladders,
    since the categories endpoint requires authentication. Cached a week."""
    if region is None:
        region = get_region_from_settings()
    cached = _categories_cache.get(_CATEGORY_TTL_SECONDS, f"browse-cats:{region}")
    if cached:
        return cached

    base_url = f"https://api.audible{audible_regions[region]}/1.0/catalog/products"

    async def harvest(keyword: str) -> dict[str, str]:
        found: dict[str, str] = {}
        try:
            async with client_session.get(
                base_url,
                params={
                    "keywords": keyword,
                    "num_results": 20,
                    "response_groups": "category_ladders",
                },
            ) as r:
                if not r.ok:
                    return found
                page = _LadderPage.model_validate(await r.json())
        except Exception as e:
            logger.debug("Category harvest failed", keyword=keyword, error=str(e))
            return found
        for product in page.products:
            for ladder in product.category_ladders:
                if ladder.ladder:
                    root = ladder.ladder[0]
                    found[root.id] = root.name
        return found

    results = await asyncio.gather(*[harvest(k) for k in _SEED_KEYWORDS])
    merged: dict[str, str] = {}
    for found in results:
        merged.update(found)
    categories = sorted(
        (BrowseCategory(id=i, name=n) for i, n in merged.items()),
        key=lambda c: c.name,
    )
    if categories:
        _categories_cache.set(categories, f"browse-cats:{region}")
    return categories


async def browse_catalog(
    client_session: ClientSession,
    region: audible_region_type | None = None,
    category_id: str = "",
    sort: str = "Relevance",
    page: int = 0,
    keywords: str = "",
) -> tuple[list[BrowseBook], int]:
    """Page through Audible's full catalog with category + sort filters."""
    if region is None:
        region = get_region_from_settings()
    if sort not in _VALID_SORTS:
        sort = "Relevance"
    base_url = f"https://api.audible{audible_regions[region]}/1.0/catalog/products"
    params: dict[str, str | int] = {
        "num_results": _PAGE_SIZE,
        "page": max(0, page),
        "products_sort_by": sort,
        "response_groups": "media,rating",
    }
    if category_id.isdigit():
        params["category_id"] = category_id
    if keywords.strip():
        params["keywords"] = keywords.strip()

    try:
        async with client_session.get(base_url, params=params) as r:
            r.raise_for_status()
            catalog = _CatalogPage.model_validate(await r.json())
    except Exception as e:
        logger.warning("Browse catalog fetch failed", error=str(e))
        return [], 0

    books: list[BrowseBook] = []
    for product in catalog.products:
        try:
            rating = product.rating.overall_distribution if product.rating else None
            books.append(
                BrowseBook(
                    book=product.to_audiobook(region),
                    rating_value=rating.display_average_rating if rating else None,
                    rating_count=rating.num_ratings if rating else None,
                )
            )
        except Exception as e:
            logger.debug("Skipping unparseable browse product", error=str(e))
    return books, catalog.total_results
