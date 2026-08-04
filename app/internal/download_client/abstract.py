from abc import ABC, abstractmethod

import pydantic

from app.internal.models import DownloadStateEnum, TorrentSource


class DownloadClientError(Exception):
    pass


class ClientItemStatus(pydantic.BaseModel):
    """Live status of a single download as reported by the download client."""

    download_id: str  # torrent info-hash (lowercase)
    name: str
    state: DownloadStateEnum
    progress: float  # 0.0 - 1.0
    size: int  # bytes
    download_speed: int | None = None  # bytes/s
    eta_seconds: int | None = None
    content_path: str | None = None  # path of the (root) downloaded content


class ClientFile(pydantic.BaseModel):
    """One file inside a torrent, as reported by the client."""

    name: str
    size: int  # bytes


class DownloadClient(ABC):
    """Minimal interface ABR needs from a download client (Radarr-style)."""

    @abstractmethod
    async def test_connection(self) -> str:
        """Verify connectivity and credentials. Returns a version string."""
        ...

    @abstractmethod
    async def add_torrent(
        self,
        source: TorrentSource,
        torrent_bytes: bytes | None,
        category: str,
        stopped: bool = False,
    ) -> str:
        """Add a torrent (magnet or .torrent file content) and return its info-hash.

        With stopped=True the torrent is added but not started, so its file list
        can be inspected before any content is downloaded.
        """
        ...

    @abstractmethod
    async def list_files(self, download_id: str) -> list[ClientFile]:
        """Files inside a torrent. Empty while metadata is still being fetched."""
        ...

    @abstractmethod
    async def start(self, download_id: str) -> None:
        """Start a torrent that was added stopped."""
        ...

    @abstractmethod
    async def list_items(self, category: str) -> list[ClientItemStatus]:
        """List all downloads in the given category."""
        ...

    @abstractmethod
    async def remove(self, download_id: str, delete_files: bool = False) -> None: ...
