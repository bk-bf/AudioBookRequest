"""Verify a finished download really is the book that was requested.

Fuzzy title matching is what lets the wrong release through in the first place
(partial_ratio counts containment as a match, so "Endymion" matches "The Rise
of Endymion"), so verifying with more fuzzy matching would just repeat the
mistake. Audible gives an exact runtime for every book, and total audio
duration is cheap to measure and hard to fake: a wrong book, an abridged
edition sold as unabridged, or a half-ripped folder all miss it badly.

Books without a known runtime (manual requests) are accepted on the presence of
audio alone - there is nothing to compare against.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile  # pyright: ignore[reportUnknownVariableType]

from app.internal.models import Audiobook, ManualBookRequest
from app.util.log import logger

AUDIO_EXTENSIONS = {".m4b", ".m4a", ".mp3", ".flac", ".ogg", ".opus", ".aac", ".wma"}

# An unabridged reading of the same text still varies by narrator and edition,
# and Audible's own runtime rounds to the minute, so the window is generous.
# It is here to catch "wrong book" and "half the files", not minor drift.
DURATION_TOLERANCE = 0.25  # ±25%
# Below this there is nothing meaningful to compare - treat as unknown.
MIN_COMPARABLE_MINUTES = 5


VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".m4v",
    ".mov",
    ".wmv",
    ".flv",
    ".mpg",
    ".mpeg",
    ".vob",
    ".ogv",
    ".rmvb",
    ".ts",
}

# Lowest believable bitrate per format, in kbps. Lossless formats are the whole
# point of this table: 527 MB of FLAC is about 2.5 hours, so a 20h book cannot
# be in there however plausible the total size looks for a lossy encode.
MIN_KBPS_BY_EXT = {
    ".flac": 400,
    ".wav": 700,
    ".alac": 400,
    ".ape": 400,
    ".m4b": 24,
    ".m4a": 24,
    ".mp3": 24,
    ".aac": 24,
    ".ogg": 24,
    ".opus": 16,
    ".wma": 24,
}
# A stray sample clip in an otherwise fine torrent should not veto it.
VIDEO_SHARE_VETO = 0.25


@dataclass
class VerifyResult:
    ok: bool
    reason: str
    measured_minutes: float | None = None
    expected_minutes: int | None = None


def measure_duration_minutes(path: Path) -> tuple[float, int]:
    """-> (total minutes, number of audio files). Files mutagen cannot parse
    are counted but contribute no duration."""
    total_seconds = 0.0
    count = 0
    for f in sorted(path.rglob("*") if path.is_dir() else [path]):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        count += 1
        try:
            audio = MutagenFile(f)
            length = getattr(getattr(audio, "info", None), "length", None)
            if isinstance(length, (int, float)) and length > 0:
                total_seconds += float(length)
        except Exception as e:  # a corrupt file must not abort the import
            logger.debug("Could not read audio duration", file=str(f), error=str(e))
    return total_seconds / 60, count


def verify_download(
    path: Path, book: Audiobook | ManualBookRequest | None
) -> VerifyResult:
    if not path.exists():
        return VerifyResult(False, f"Download path does not exist: {path}")

    measured, count = measure_duration_minutes(path)
    if count == 0:
        return VerifyResult(False, "No audio files in the download")

    expected = getattr(book, "runtime_length_min", None)
    if not isinstance(expected, int) or expected < MIN_COMPARABLE_MINUTES:
        return VerifyResult(
            True,
            f"{count} audio file(s), no expected runtime to compare against",
            measured_minutes=measured,
        )

    if measured < MIN_COMPARABLE_MINUTES:
        return VerifyResult(
            True,
            f"{count} audio file(s), duration unreadable - accepted on file presence",
            measured_minutes=measured,
            expected_minutes=expected,
        )

    low = expected * (1 - DURATION_TOLERANCE)
    high = expected * (1 + DURATION_TOLERANCE)
    if not (low <= measured <= high):
        return VerifyResult(
            False,
            (
                f"Runtime mismatch: {measured / 60:.1f}h of audio, expected "
                f"~{expected / 60:.1f}h (±{DURATION_TOLERANCE:.0%}). "
                "Likely the wrong book or an incomplete rip."
            ),
            measured_minutes=measured,
            expected_minutes=expected,
        )

    return VerifyResult(
        True,
        f"{count} audio file(s), {measured / 60:.1f}h matches the expected {expected / 60:.1f}h",
        measured_minutes=measured,
        expected_minutes=expected,
    )


def inspect_file_list(
    files: "list[tuple[str, int]]", runtime_minutes: int | None
) -> str | None:
    """Judge a torrent from its file list alone, before any content is
    downloaded. -> rejection reason, or None to proceed.

    This is the only check that can see inside a magnet link. Magnets carry no
    file list, so qBittorrent is asked to fetch just the metadata (a few KB)
    and the payload is judged from that.

    It is decisive where title and total size are not: 527 MB of FLAC is about
    2.5 hours of audio, so a music album cannot masquerade as a 20-hour book
    even though its total size implies a perfectly plausible 57 kbps if you
    assume the wrong format.
    """
    if not files:
        return None  # metadata not in yet; nothing to judge

    total = sum(size for _, size in files)
    audio_bytes = 0
    video_bytes = 0
    by_ext: dict[str, int] = {}
    for name, size in files:
        ext = os.path.splitext(name)[1].lower()
        if ext in AUDIO_EXTENSIONS:
            audio_bytes += size
            by_ext[ext] = by_ext.get(ext, 0) + size
        elif ext in VIDEO_EXTENSIONS:
            video_bytes += size

    if audio_bytes == 0:
        return f"No audio files in the torrent ({len(files)} file(s))"

    if total > 0 and video_bytes / total > VIDEO_SHARE_VETO:
        return (
            f"{video_bytes / total:.0%} of the torrent is video "
            f"({video_bytes / 1e6:.0f} MB) - not an audiobook"
        )

    if not runtime_minutes or runtime_minutes < MIN_COMPARABLE_MINUTES:
        return None

    # judge against the dominant audio format's floor
    dominant = max(by_ext, key=lambda e: by_ext[e])
    min_kbps = MIN_KBPS_BY_EXT.get(dominant, 24)
    min_bytes = runtime_minutes * 60 * (min_kbps * 1000 / 8)
    if audio_bytes < min_bytes:
        implied_hours = audio_bytes * 8 / (min_kbps * 1000) / 3600
        return (
            f"{audio_bytes / 1e6:.0f} MB of {dominant.lstrip('.')} is at most "
            f"~{implied_hours:.1f}h of audio, but the book is "
            f"{runtime_minutes / 60:.1f}h"
        )
    return None
