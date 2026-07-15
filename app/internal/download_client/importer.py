import os
import re
import shutil
from pathlib import Path

from aiohttp import ClientSession
from sqlmodel import Session

from app.internal.audiobookshelf.client import abs_trigger_scan
from app.internal.audiobookshelf.config import abs_config
from app.internal.download_client.config import dc_config
from app.internal.models import (
    Audiobook,
    DownloadQueueItem,
    DownloadStateEnum,
    EventEnum,
    ManualBookRequest,
)
from app.internal.notifications import (
    send_all_manual_notifications,
    send_all_notifications,
)
from app.util.log import logger

AUDIO_EXTENSIONS = {".m4b", ".m4a", ".mp3", ".flac", ".ogg", ".opus", ".aac", ".wma"}
EXTRA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".cue", ".nfo", ".pdf", ".epub", ".txt"}


def sanitize_path_part(part: str) -> str:
    """Make a metadata string safe to use as a single directory/file name."""
    part = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", part)
    part = part.strip(" .")
    return part[:120] or "Unknown"


def _collect_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    files: list[Path] = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            files.append(Path(dirpath) / name)
    return files


def _link_or_copy(src: Path, dst: Path) -> str:
    """Hardlink src to dst, falling back to copy across filesystems.
    Returns 'hardlink', 'copy' or 'skipped'."""
    if dst.exists():
        if dst.stat().st_size == src.stat().st_size:
            return "skipped"
        dst.unlink()
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def import_files(source_path: Path, dest_dir: Path) -> int:
    """Hardlink/copy all relevant files from a finished download into dest_dir.
    Returns the number of audio files imported."""
    all_files = _collect_files(source_path)
    audio_files = [f for f in all_files if f.suffix.lower() in AUDIO_EXTENSIONS]
    extra_files = [f for f in all_files if f.suffix.lower() in EXTRA_EXTENSIONS]
    if not audio_files:
        raise FileNotFoundError(f"No audio files found in {source_path}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in audio_files + extra_files:
        method = _link_or_copy(f, dest_dir / f.name)
        logger.debug(
            "Imported file", src=str(f), dest=str(dest_dir / f.name), method=method
        )
    return len(audio_files)


async def import_queue_item(
    session: Session,
    client_session: ClientSession,
    item: DownloadQueueItem,
) -> bool:
    """Import a completed download into the library (Radarr-style).

    On success: files are hardlinked/copied to <library_dir>/<Author>/<Title>/,
    the book is marked downloaded, and an ABS scan is triggered.
    """
    book: Audiobook | ManualBookRequest | None = None
    if item.asin:
        book = session.get(Audiobook, item.asin)
    elif item.manual_request_id:
        book = session.get(ManualBookRequest, item.manual_request_id)
    if book is None:
        item.state = DownloadStateEnum.error
        item.error = "Book for this download no longer exists"
        session.add(item)
        session.commit()
        return False

    library_dir = dc_config.get_library_dir(session)
    if not library_dir or not item.save_path:
        item.state = DownloadStateEnum.error
        item.error = "Library directory or download path missing"
        session.add(item)
        session.commit()
        return False

    source_path = Path(dc_config.map_path(session, item.save_path))
    if not source_path.exists():
        item.state = DownloadStateEnum.error
        item.error = (
            f"Download path not found: {source_path}. "
            "Check volume mounts / remote path mapping."
        )
        session.add(item)
        session.commit()
        logger.error("Import failed: path not visible to ABR", path=str(source_path))
        return False

    author = sanitize_path_part(book.authors[0] if book.authors else "Unknown Author")
    title = sanitize_path_part(book.title)
    dest_dir = Path(library_dir) / author / title

    try:
        count = import_files(source_path, dest_dir)
    except Exception as e:
        item.state = DownloadStateEnum.error
        item.error = f"Import failed: {e}"
        session.add(item)
        session.commit()
        logger.error("Import failed", error=str(e), source=str(source_path))
        return False

    item.state = DownloadStateEnum.imported
    item.import_path = str(dest_dir)
    item.error = None
    item.progress = 1.0
    book.downloaded = True
    book.downloaded_path = str(dest_dir)
    session.add(item)
    session.add(book)
    session.commit()
    logger.info(
        "Imported download into library",
        title=book.title,
        dest=str(dest_dir),
        audio_files=count,
    )

    # Only now is the download *actually* done -> notify + rescan
    try:
        if abs_config.is_valid(session):
            await abs_trigger_scan(session, client_session)
    except Exception as e:
        logger.warning("Failed to trigger ABS scan after import", error=str(e))

    replacements = {
        "importPath": str(dest_dir),
        "sourceTitle": item.source_title,
        "indexerName": item.indexer,
    }
    try:
        if isinstance(book, ManualBookRequest):
            await send_all_manual_notifications(
                EventEnum.on_successful_download, book, replacements
            )
        else:
            await send_all_notifications(
                EventEnum.on_successful_download, book.asin, replacements
            )
    except Exception as e:
        logger.warning("Failed to send import notification", error=str(e))

    return True
