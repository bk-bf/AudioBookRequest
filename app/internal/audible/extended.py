from aiohttp import ClientSession
from pydantic import BaseModel

from app.internal.audible.types import (
    audible_region_type,
    audible_regions,
    get_region_from_settings,
)
from app.util.cache import SimpleCache
from app.util.log import logger

_EXTENDED_TTL_SECONDS = 60 * 60 * 24


class ExtendedMetadata(BaseModel):
    """Extra, display-only metadata for the book detail page."""

    publisher_name: str | None = None
    publisher_summary: str | None = None  # HTML from Audible
    language: str | None = None
    rating_value: float | None = None
    rating_count: int | None = None
    series_name: str | None = None
    series_sequence: str | None = None
    genres: list[str] = []


class _Rating(BaseModel):
    class _Distribution(BaseModel):
        display_average_rating: float | None = None
        num_ratings: int | None = None

    overall_distribution: _Distribution = _Distribution()


class _Series(BaseModel):
    title: str | None = None
    sequence: str | None = None


class _CategoryLadder(BaseModel):
    class _Category(BaseModel):
        name: str

    ladder: list[_Category] = []


class _ExtendedProduct(BaseModel):
    publisher_name: str | None = None
    publisher_summary: str | None = None
    language: str | None = None
    rating: _Rating | None = None
    series: list[_Series] | None = None
    category_ladders: list[_CategoryLadder] | None = None


class _ExtendedResponse(BaseModel):
    product: _ExtendedProduct


extended_metadata_cache: SimpleCache[ExtendedMetadata, str] = SimpleCache()


async def get_extended_metadata(
    client_session: ClientSession,
    asin: str,
    audible_region: audible_region_type | None = None,
) -> ExtendedMetadata | None:
    """Fetch display-only extended metadata for a book. Cached in memory,
    returns None on any failure so pages degrade gracefully."""
    cached = extended_metadata_cache.get(_EXTENDED_TTL_SECONDS, asin)
    if cached:
        return cached

    if audible_region is None:
        audible_region = get_region_from_settings()
    base_url = f"https://api.audible{audible_regions[audible_region]}/1.0/catalog/products/{asin}"
    params = {
        "response_groups": "contributors,product_attrs,product_desc,product_extended_attrs,rating,series,category_ladders",
    }
    try:
        async with client_session.get(base_url, params=params) as response:
            response.raise_for_status()
            product = _ExtendedResponse.model_validate(await response.json()).product
    except Exception as e:
        logger.warning(
            "Failed to fetch extended audible metadata", asin=asin, error=str(e)
        )
        return None

    genres: list[str] = []
    for ladder in product.category_ladders or []:
        for category in ladder.ladder:
            if category.name not in genres:
                genres.append(category.name)

    series = (product.series or [None])[0]
    rating = product.rating or _Rating()

    metadata = ExtendedMetadata(
        publisher_name=product.publisher_name,
        publisher_summary=product.publisher_summary,
        language=product.language.capitalize() if product.language else None,
        rating_value=rating.overall_distribution.display_average_rating,
        rating_count=rating.overall_distribution.num_ratings,
        series_name=series.title if series else None,
        series_sequence=series.sequence if series else None,
        genres=genres[:8],
    )
    extended_metadata_cache.set(metadata, asin)
    return metadata
