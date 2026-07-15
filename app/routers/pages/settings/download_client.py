from typing import Annotated

from fastapi import APIRouter, Depends, Form, Security
from sqlmodel import Session

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.download_client.monitor import monitor_lifespan
from app.internal.models import GroupEnum
from app.routers.api.settings.download_client import (
    UpdateDownloadClientSettings,
    get_download_client_settings as api_get_settings,
    test_download_client_connection as api_test_connection,
    update_download_client_settings as api_update_settings,
)
from app.util.db import get_session
from app.util.templates import catalog_response
from app.util.toast import ToastException

router = APIRouter(prefix="/download-client", lifespan=monitor_lifespan)


@router.get("")
def read_download_client(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
):
    settings = api_get_settings(session, admin_user)
    return catalog_response(
        "Settings.DownloadClient",
        user=admin_user,
        settings=settings,
    )


@router.post("/hx-update")
def update_download_client(
    dc_base_url: Annotated[str, Form()],
    dc_library_dir: Annotated[str, Form()],
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    dc_enabled: Annotated[bool, Form()] = False,
    dc_username: Annotated[str, Form()] = "",
    dc_password: Annotated[str, Form()] = "",
    dc_category: Annotated[str, Form()] = "abr",
    dc_path_map_from: Annotated[str, Form()] = "",
    dc_path_map_to: Annotated[str, Form()] = "",
    dc_auto_retry_minutes: Annotated[int, Form()] = 60,
):
    api_update_settings(
        UpdateDownloadClientSettings(
            dc_enabled=dc_enabled,
            dc_base_url=dc_base_url,
            dc_username=dc_username,
            dc_password=dc_password,
            dc_category=dc_category,
            dc_library_dir=dc_library_dir,
            dc_path_map_from=dc_path_map_from,
            dc_path_map_to=dc_path_map_to,
            dc_auto_retry_minutes=dc_auto_retry_minutes,
        ),
        session,
        admin_user,
    )
    raise ToastException("Download client settings saved", "success")


@router.post("/hx-test")
async def test_download_client(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
):
    try:
        result = await api_test_connection(session, admin_user)
    except Exception as e:
        detail = getattr(e, "detail", str(e))
        raise ToastException(f"Connection failed: {detail}", "error") from None
    raise ToastException(f"Connected to qBittorrent {result.version}", "success")
