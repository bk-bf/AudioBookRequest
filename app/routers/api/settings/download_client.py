from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, Security
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.auth.authentication import AnyAuth, DetailedUser
from app.internal.download_client.abstract import DownloadClientError
from app.internal.download_client.config import dc_config
from app.internal.download_client.grab import get_download_client
from app.internal.download_client.qbittorrent import QbittorrentClient
from app.internal.models import GroupEnum
from app.util.db import get_session

router = APIRouter(prefix="/download-client")


class DownloadClientSettings(BaseModel):
    dc_enabled: bool
    dc_type: str
    dc_base_url: str
    dc_username: str
    dc_has_password: bool
    dc_category: str
    dc_library_dir: str
    dc_path_map_from: str
    dc_path_map_to: str
    dc_auto_retry_minutes: int


@router.get("", response_model=DownloadClientSettings)
def get_download_client_settings(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    return DownloadClientSettings(
        dc_enabled=dc_config.get_enabled(session),
        dc_type=dc_config.get_type(session),
        dc_base_url=dc_config.get_base_url(session) or "",
        dc_username=dc_config.get_username(session) or "",
        dc_has_password=bool(dc_config.get_password(session)),
        dc_category=dc_config.get_category(session),
        dc_library_dir=dc_config.get_library_dir(session) or "",
        dc_path_map_from=dc_config.get_path_map_from(session) or "",
        dc_path_map_to=dc_config.get_path_map_to(session) or "",
        dc_auto_retry_minutes=dc_config.get_auto_retry_minutes(session),
    )


class UpdateDownloadClientSettings(BaseModel):
    dc_enabled: bool
    dc_base_url: str
    dc_username: str = ""
    dc_password: str = ""  # empty = keep existing
    dc_category: str = "abr"
    dc_library_dir: str
    dc_path_map_from: str = ""
    dc_path_map_to: str = ""
    dc_auto_retry_minutes: int = 60


@router.patch("", status_code=204)
def update_download_client_settings(
    body: UpdateDownloadClientSettings,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    dc_config.set_enabled(session, body.dc_enabled)
    dc_config.set_type(session, "qbittorrent")
    dc_config.set_base_url(session, body.dc_base_url.strip())
    dc_config.set_username(session, body.dc_username.strip())
    if body.dc_password:
        dc_config.set_password(session, body.dc_password)
    dc_config.set_category(session, body.dc_category.strip() or "abr")
    dc_config.set_library_dir(session, body.dc_library_dir.strip())
    dc_config.set_path_map_from(session, body.dc_path_map_from.strip())
    dc_config.set_path_map_to(session, body.dc_path_map_to.strip())
    dc_config.set_auto_retry_minutes(session, max(1, body.dc_auto_retry_minutes))
    return Response(status_code=204)


class TestConnectionResponse(BaseModel):
    version: str


@router.get("/test-connection", response_model=TestConnectionResponse)
async def test_download_client_connection(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    client = get_download_client(session)
    if client is None:
        # allow testing before enabling: build directly from stored values
        base_url = dc_config.get_base_url(session)
        if not base_url:
            raise HTTPException(status_code=400, detail="Base URL not set")
        client = QbittorrentClient(
            base_url=base_url,
            username=dc_config.get_username(session),
            password=dc_config.get_password(session),
        )
    try:
        version = await client.test_connection()
    except DownloadClientError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Connection failed: {e}")
    return TestConnectionResponse(version=version)
