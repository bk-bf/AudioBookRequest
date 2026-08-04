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
