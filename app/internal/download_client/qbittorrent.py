import base64
from typing import cast, final, override
from urllib.parse import parse_qs

import aiohttp
from aiohttp import ClientSession, FormData
from pydantic import BaseModel, TypeAdapter

from app.internal.download_client.abstract import (
    ClientFile,
    ClientItemStatus,
    DownloadClient,
    DownloadClientError,
)
from app.internal.models import DownloadStateEnum, TorrentSource
from app.util.connection import USER_AGENT
from app.util.log import logger

# https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0)
_STATE_MAP: dict[str, DownloadStateEnum] = {
    "error": DownloadStateEnum.error,
    "missingFiles": DownloadStateEnum.error,
    "uploading": DownloadStateEnum.completed,
    "stoppedUP": DownloadStateEnum.completed,
    "pausedUP": DownloadStateEnum.completed,
    "queuedUP": DownloadStateEnum.completed,
    "stalledUP": DownloadStateEnum.completed,
    "checkingUP": DownloadStateEnum.completed,
    "forcedUP": DownloadStateEnum.completed,
    "allocating": DownloadStateEnum.downloading,
    "downloading": DownloadStateEnum.downloading,
    "metaDL": DownloadStateEnum.downloading,
    "forcedDL": DownloadStateEnum.downloading,
    "moving": DownloadStateEnum.downloading,
    "checkingDL": DownloadStateEnum.downloading,
    "checkingResumeData": DownloadStateEnum.downloading,
    "stalledDL": DownloadStateEnum.stalled,
    "stoppedDL": DownloadStateEnum.queued,
    "pausedDL": DownloadStateEnum.queued,
    "queuedDL": DownloadStateEnum.queued,
    "unknown": DownloadStateEnum.queued,
}


class _QbtTorrentInfo(BaseModel):
    hash: str
    name: str = ""
    state: str = "unknown"
    progress: float = 0.0
    size: int = 0
    dlspeed: int = 0
    eta: int | None = None
    content_path: str | None = None


_QbtTorrentList = TypeAdapter(list[_QbtTorrentInfo])


def parse_magnet_info_hash(magnet_url: str) -> str | None:
    """Extract the btih info-hash from a magnet link as lowercase hex.

    Handles url-encoded params and base32-encoded hashes."""
    if not magnet_url.startswith("magnet:?"):
        return None
    params = parse_qs(magnet_url.removeprefix("magnet:?"))
    for xt in params.get("xt", []):
        if not xt.startswith("urn:btih:"):
            continue
        info_hash = xt.removeprefix("urn:btih:")
        if len(info_hash) == 32:  # base32 variant
            try:
                return base64.b32decode(info_hash.upper()).hex()
            except Exception:
                return None
        return info_hash.lower()
    return None


@final
class QbittorrentClient(DownloadClient):
    def __init__(self, base_url: str, username: str | None, password: str | None):
        self.base_url = base_url.rstrip("/")
        self.username = username or ""
        self.password = password or ""

    async def _login(self, client: ClientSession):
        async with client.post(
            f"{self.base_url}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
            headers={"User-Agent": USER_AGENT, "Referer": self.base_url},
        ) as r:
            text = (await r.text()).strip()
            # Success is 200 with body "Ok."; behind an auth subnet whitelist
            # qBittorrent instead answers 204 with an empty body.
            if not r.ok or text == "Fails.":
                raise DownloadClientError(
                    f"qBittorrent login failed: {r.status} {text[:100]}"
                )

    def _session(self) -> ClientSession:
        # cookie_jar keeps the SID cookie from login; unsafe allows IP-address hosts
        return ClientSession(
            timeout=aiohttp.ClientTimeout(30),
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )

    @override
    async def test_connection(self) -> str:
        async with self._session() as client:
            await self._login(client)
            async with client.get(
                f"{self.base_url}/api/v2/app/version",
                headers={"User-Agent": USER_AGENT},
            ) as r:
                if not r.ok:
                    raise DownloadClientError(
                        f"qBittorrent version check failed: {r.status}"
                    )
                return await r.text()

    @override
    async def add_torrent(
        self,
        source: TorrentSource,
        torrent_bytes: bytes | None,
        category: str,
        stopped: bool = False,
    ) -> str:
        """Add a torrent to qBittorrent. Prefers the raw .torrent (reliable info-hash),
        falls back to the magnet link."""
        if torrent_bytes is None and not source.magnet_url:
            raise DownloadClientError("Source has neither torrent file nor magnet url")

        form = FormData()
        form.add_field("category", category)
        if stopped:
            # qBit 5 renamed 'paused' to 'stopped'; send both so this works
            # either side of the rename. Unknown fields are ignored.
            form.add_field("stopped", "true")
            form.add_field("paused", "true")
        if torrent_bytes is not None:
            import torf

            try:
                info_hash = str(
                    torf.Torrent.read_stream(torrent_bytes).infohash
                ).lower()
            except Exception as e:
                raise DownloadClientError(f"Failed to parse torrent file: {e}") from e
            form.add_field(
                "torrents",
                torrent_bytes,
                filename="abr.torrent",
                content_type="application/x-bittorrent",
            )
        else:
            assert source.magnet_url is not None
            magnet_hash = parse_magnet_info_hash(source.magnet_url)
            if not magnet_hash:
                raise DownloadClientError("Could not parse info-hash from magnet url")
            info_hash = magnet_hash
            form.add_field("urls", source.magnet_url)

        async with self._session() as client:
            await self._login(client)
            async with client.post(
                f"{self.base_url}/api/v2/torrents/add",
                data=form,
                headers={"User-Agent": USER_AGENT},
            ) as r:
                text = await r.text()
                if r.status == 409:
                    # Torrent already in the client (e.g. added manually):
                    # adopt it into our category so it gets tracked + imported.
                    logger.info(
                        "Torrent already in qBittorrent, adopting",
                        info_hash=info_hash,
                        title=source.title,
                    )
                    await self._set_category(client, info_hash, category)
                    return info_hash
                if not r.ok or text.strip() == "Fails.":
                    raise DownloadClientError(
                        f"qBittorrent rejected torrent: {r.status} {text.strip()[:100]}"
                    )

        logger.info(
            "Added torrent to qBittorrent",
            info_hash=info_hash,
            title=source.title,
            category=category,
        )
        return info_hash

    async def _set_category(
        self, client: ClientSession, info_hash: str, category: str
    ) -> None:
        # the category may not exist yet if nothing was ever added with it
        async with client.post(
            f"{self.base_url}/api/v2/torrents/createCategory",
            data={"category": category, "savePath": ""},
            headers={"User-Agent": USER_AGENT},
        ) as r:
            if not r.ok and r.status != 409:  # 409 = already exists
                logger.warning("Failed to create qBittorrent category", status=r.status)
        async with client.post(
            f"{self.base_url}/api/v2/torrents/setCategory",
            data={"hashes": info_hash, "category": category},
            headers={"User-Agent": USER_AGENT},
        ) as r:
            if not r.ok:
                raise DownloadClientError(
                    f"Failed to adopt existing torrent into category: {r.status}"
                )

    @override
    async def list_items(self, category: str) -> list[ClientItemStatus]:
        async with self._session() as client:
            await self._login(client)
            async with client.get(
                f"{self.base_url}/api/v2/torrents/info",
                params={"category": category},
                headers={"User-Agent": USER_AGENT},
            ) as r:
                if not r.ok:
                    raise DownloadClientError(
                        f"qBittorrent torrents/info failed: {r.status}"
                    )
                try:
                    torrents = _QbtTorrentList.validate_python(await r.json())
                except Exception as e:
                    raise DownloadClientError(
                        f"Failed to parse qBittorrent torrent list: {e}"
                    ) from e

        return [
            ClientItemStatus(
                download_id=t.hash.lower(),
                name=t.name,
                state=_STATE_MAP.get(t.state, DownloadStateEnum.queued),
                progress=t.progress,
                size=t.size,
                download_speed=t.dlspeed or None,
                # qBittorrent reports 8640000 as "infinite" eta
                eta_seconds=(t.eta if t.eta is not None and t.eta < 8640000 else None),
                content_path=t.content_path or None,
            )
            for t in torrents
        ]

    @override
    @override
    async def list_files(self, download_id: str) -> list[ClientFile]:
        """Files inside a torrent. Returns [] while qBittorrent is still
        fetching metadata - a magnet arrives without any file list."""
        async with self._session() as client:
            await self._login(client)
            async with client.get(
                f"{self.base_url}/api/v2/torrents/files",
                params={"hash": download_id},
                headers={"User-Agent": USER_AGENT},
            ) as r:
                if r.status == 404:
                    return []
                if not r.ok:
                    raise DownloadClientError(
                        f"qBittorrent files lookup failed: {r.status}"
                    )
                try:
                    payload = cast(object, await r.json(content_type=None))
                except Exception:
                    return []
        if not isinstance(payload, list):
            return []
        files: list[ClientFile] = []
        for entry in cast(list[object], payload):
            if not isinstance(entry, dict):
                continue
            fields = cast(dict[str, object], entry)
            name = fields.get("name")
            size = fields.get("size")
            if isinstance(name, str) and isinstance(size, int):
                files.append(ClientFile(name=name, size=size))
        return files

    @override
    async def start(self, download_id: str) -> None:
        """Start a torrent added with stopped=True. /start is the qBit 5 name,
        /resume the older one."""
        async with self._session() as client:
            await self._login(client)
            for endpoint in ("start", "resume"):
                async with client.post(
                    f"{self.base_url}/api/v2/torrents/{endpoint}",
                    data={"hashes": download_id},
                    headers={"User-Agent": USER_AGENT},
                ) as r:
                    if r.ok:
                        return
        raise DownloadClientError("qBittorrent refused to start the torrent")

    @override
    async def remove(self, download_id: str, delete_files: bool = False) -> None:
        async with self._session() as client:
            await self._login(client)
            async with client.post(
                f"{self.base_url}/api/v2/torrents/delete",
                data={
                    "hashes": download_id,
                    "deleteFiles": "true" if delete_files else "false",
                },
                headers={"User-Agent": USER_AGENT},
            ) as r:
                if not r.ok:
                    raise DownloadClientError(f"qBittorrent delete failed: {r.status}")
