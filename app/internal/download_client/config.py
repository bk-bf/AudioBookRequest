from typing import Literal, Optional

from sqlmodel import Session

from app.util.cache import StringConfigCache


class DownloadClientMisconfigured(ValueError):
    pass


DownloadClientConfigKey = Literal[
    "dc_enabled",
    "dc_type",
    "dc_base_url",
    "dc_username",
    "dc_password",
    "dc_category",
    "dc_library_dir",
    "dc_path_map_from",
    "dc_path_map_to",
    "dc_auto_retry_minutes",
]


class DownloadClientConfig(StringConfigCache[DownloadClientConfigKey]):
    _default_category: str = "abr"
    _default_auto_retry_minutes: int = 60

    def is_valid(self, session: Session) -> bool:
        return (
            self.get_enabled(session)
            and self.get_base_url(session) is not None
            and self.get_library_dir(session) is not None
        )

    def raise_if_invalid(self, session: Session):
        if not self.get_enabled(session):
            raise DownloadClientMisconfigured("Download client is disabled")
        if not self.get_base_url(session):
            raise DownloadClientMisconfigured("Download client base url not set")
        if not self.get_library_dir(session):
            raise DownloadClientMisconfigured("Library directory not set")

    def get_enabled(self, session: Session) -> bool:
        return bool(self.get_bool(session, "dc_enabled") or False)

    def set_enabled(self, session: Session, enabled: bool):
        self.set_bool(session, "dc_enabled", enabled)

    def get_type(self, session: Session) -> str:
        return self.get(session, "dc_type", "qbittorrent")

    def set_type(self, session: Session, client_type: str):
        self.set(session, "dc_type", client_type)

    def get_base_url(self, session: Session) -> Optional[str]:
        url = self.get(session, "dc_base_url")
        if url:
            return url.rstrip("/")
        return None

    def set_base_url(self, session: Session, base_url: str):
        self.set(session, "dc_base_url", base_url)

    def get_username(self, session: Session) -> Optional[str]:
        return self.get(session, "dc_username")

    def set_username(self, session: Session, username: str):
        self.set(session, "dc_username", username)

    def get_password(self, session: Session) -> Optional[str]:
        return self.get(session, "dc_password")

    def set_password(self, session: Session, password: str):
        self.set(session, "dc_password", password)

    def get_category(self, session: Session) -> str:
        return self.get(session, "dc_category", self._default_category)

    def set_category(self, session: Session, category: str):
        self.set(session, "dc_category", category)

    def get_library_dir(self, session: Session) -> Optional[str]:
        path = self.get(session, "dc_library_dir")
        if path:
            return path.rstrip("/")
        return None

    def set_library_dir(self, session: Session, library_dir: str):
        self.set(session, "dc_library_dir", library_dir)

    def get_path_map_from(self, session: Session) -> Optional[str]:
        return self.get(session, "dc_path_map_from")

    def set_path_map_from(self, session: Session, value: str):
        self.set(session, "dc_path_map_from", value)

    def get_path_map_to(self, session: Session) -> Optional[str]:
        return self.get(session, "dc_path_map_to")

    def set_path_map_to(self, session: Session, value: str):
        self.set(session, "dc_path_map_to", value)

    def get_auto_retry_minutes(self, session: Session) -> int:
        return self.get_int(
            session, "dc_auto_retry_minutes", self._default_auto_retry_minutes
        )

    def set_auto_retry_minutes(self, session: Session, minutes: int):
        self.set_int(session, "dc_auto_retry_minutes", minutes)

    def map_path(self, session: Session, path: str) -> str:
        """Apply the optional remote path mapping (client path -> ABR path)."""
        map_from = (self.get_path_map_from(session) or "").rstrip("/")
        map_to = (self.get_path_map_to(session) or "").rstrip("/")
        if map_from and map_to and path.startswith(map_from):
            return map_to + path[len(map_from) :]
        return path


dc_config = DownloadClientConfig()
