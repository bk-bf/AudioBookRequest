"""Tell an audiobook release apart from an ebook or video one.

Prowlarr indexers happily return non-audiobook releases for an audiobook
search, and fuzzy title matching cannot see the difference - "Virgil - The
Aeneid (EPUB) [Penguin Classics]" and "TTC VIDEO - The Aeneid of Virgil" both
match the requested book perfectly on title and author. Ranking scores them
like any other source, and with no seeders they stall forever.

Two levels of rejection:

- **Conditional** markers (ebook formats, video containers) only disqualify a
  release when nothing else says audio, because audiobook releases routinely
  bundle extras - "Unabridged M4B + EPUB", or a folder of MP3s with a stray
  .epub inside.
- **Unconditional** markers (resolutions, rip types, video codecs, the word
  "video") reject regardless. A 1080p BluRay rip is not an audiobook however
  many audio words appear in its title.
"""

import re

EBOOK_EXTENSIONS = (
    "epub",
    "pdf",
    "mobi",
    "azw",
    "azw3",
    "azw4",
    "cbr",
    "cbz",
    "djvu",
    "fb2",
    "lit",
    "pdb",
    "prc",
)
EBOOK_KEYWORDS = ("ebook", "e-book", "retail epub", "kindle", "comic", "magazine")

# Containers that are usually video but occasionally wrap audio - an audio
# marker elsewhere in the title overrides these.
VIDEO_CONTAINERS = (
    "mkv",
    "mp4",
    "avi",
    "m4v",
    "mov",
    "wmv",
    "flv",
    "mpg",
    "mpeg",
    "vob",
    "ogv",
    "rmvb",
    "webm",
)
# Nothing rescues these: no audiobook is a 1080p BluRay rip.
VIDEO_DEFINITE = (
    "video",
    "x264",
    "x265",
    "h264",
    "h265",
    "hevc",
    "xvid",
    "divx",
    "bluray",
    "blu-ray",
    "bdrip",
    "brrip",
    "dvdrip",
    "dvd",
    "hdtv",
    "webrip",
    "web-dl",
    "hdrip",
    "480p",
    "720p",
    "1080p",
    "1440p",
    "2160p",
    "4k",
)

AUDIO_EXTENSIONS = (
    "m4b",
    "m4a",
    "mp3",
    "flac",
    "ogg",
    "opus",
    "aac",
    "aax",
    "aa",
    "wma",
    "alac",
    "wav",
)
AUDIO_KEYWORDS = (
    "audiobook",
    "audio book",
    "audible",
    "unabridged",
    "abridged",
    "narrated",
    "narrator",
    "vbr",
    "kbps",
)


def _mentions(title: str, needles: tuple[str, ...]) -> str | None:
    """Match whole tokens only, so 'aa' does not fire inside 'Isaac' and '4k'
    does not fire inside a catalogue number."""
    for needle in needles:
        pattern = r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"
        if re.search(pattern, title):
            return needle
    return None


def rejection(title: str) -> tuple[str, str] | None:
    """-> (kind, marker) when the release should never be grabbed for an
    audiobook request, else None."""
    lowered = title.lower()

    definite_video = _mentions(lowered, VIDEO_DEFINITE)
    if definite_video:
        return "video", definite_video

    # 'cbr' is both a comic format and a bitrate mode, and 'mp4' sometimes
    # wraps audio - resolve the audio side before the conditional markers
    if _mentions(lowered, AUDIO_EXTENSIONS) or _mentions(lowered, AUDIO_KEYWORDS):
        return None

    container = _mentions(lowered, VIDEO_CONTAINERS)
    if container:
        return "video", container

    ebook = _mentions(lowered, EBOOK_EXTENSIONS) or _mentions(lowered, EBOOK_KEYWORDS)
    if ebook:
        return "ebook", ebook
    return None


def is_rejected_release(title: str) -> bool:
    return rejection(title) is not None


# Spoken-word releases are typically 32-64 kbps mono; this sits below anything
# genuine while still catching a music album standing in for a long book.
MIN_PLAUSIBLE_KBPS = 24
MIN_COMPARABLE_MINUTES = 30


def implausible_size(size_bytes: int, runtime_minutes: int | None) -> str | None:
    """Reject a release that cannot possibly contain the book, whatever its
    title claims.

    Title filtering cannot separate an audiobook from a music album of the same
    name - "Lady of the Lake" returns four albums and a single classical track,
    none of them the book. But a 12-hour audiobook cannot fit in 40 MB, and
    that is decidable before grabbing anything.

    Only a floor is applied. An upper bound would misfire on lossless rips,
    which are legitimately enormous - so a big mislabelled release (a FLAC
    album, say) still gets through here and is caught after download by the
    runtime check in download_client/verify.py.
    """
    if not runtime_minutes or runtime_minutes < MIN_COMPARABLE_MINUTES:
        return None
    if size_bytes <= 0:
        return None  # size unknown - not evidence of anything
    min_bytes = runtime_minutes * 60 * (MIN_PLAUSIBLE_KBPS * 1000 / 8)
    if size_bytes < min_bytes:
        return (
            f"{size_bytes / 1e6:.0f} MB is too small for a "
            f"{runtime_minutes / 60:.1f}h book (needs ~{min_bytes / 1e6:.0f} MB "
            f"even at {MIN_PLAUSIBLE_KBPS} kbps)"
        )
    return None
