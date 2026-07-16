import posixpath
import re
from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, HTTPException, Response, Security
from sqlmodel import Session

from app.internal.audiobookshelf.config import abs_config
from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.util.connection import USER_AGENT, get_connection
from app.util.db import get_session

router = APIRouter(prefix="/abs")

_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@router.get("/cover/{item_id}")
async def abs_cover(
    item_id: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(ABRAuth())],
):
    """Proxy Audiobookshelf cover images: the ABS base url is typically an
    internal docker hostname the browser can't reach."""
    _ = user
    if not _ITEM_ID_RE.match(item_id):
        raise HTTPException(status_code=400, detail="Invalid item id")
    base_url = abs_config.get_base_url(session)
    token = abs_config.get_api_token(session)
    if not base_url or not token:
        raise HTTPException(status_code=404, detail="Audiobookshelf not configured")
    url = posixpath.join(base_url, f"api/items/{item_id}/cover")
    async with client_session.get(
        url,
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
        params={"width": "480"},
    ) as resp:
        if not resp.ok:
            raise HTTPException(status_code=404, detail="Cover not found")
        content = await resp.read()
        return Response(
            content=content,
            media_type=resp.headers.get("Content-Type", "image/jpeg"),
            headers={"Cache-Control": "public, max-age=86400"},
        )
