"""Tell an ebook release apart from an audiobook one.

Prowlarr indexers happily return ebook releases for an audiobook search, and
fuzzy title matching cannot see the difference - "Virgil - The Aeneid (EPUB)
[Penguin Classics]" matches the book perfectly on title and author. Ranking
scores it like any other source, and with no seeders it stalls forever.

The check is deliberately asymmetric: an ebook marker only disqualifies a
release when there is *no* audio marker, because audiobook releases routinely
bundle the ebook as a bonus ("Unabridged M4B + EPUB", or a folder of MP3s with
a stray .epub inside).
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
    "cbr kbps",  # bitrate mention, not the comic format
    "kbps",
)


def _mentions(title: str, needles: tuple[str, ...]) -> str | None:
    """Match whole words/tokens only, so 'aa' does not fire inside 'Isaac'."""
    for needle in needles:
        pattern = r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"
        if re.search(pattern, title):
            return needle
    return None


def ebook_marker(title: str) -> str | None:
    """The ebook marker that disqualifies this release, if any."""
    lowered = title.lower()
    # 'cbr' is both a comic format and a bitrate mode; a bitrate mention makes
    # it audio, so resolve the audio side first
    audio = _mentions(lowered, AUDIO_EXTENSIONS) or _mentions(lowered, AUDIO_KEYWORDS)
    if audio:
        return None
    return _mentions(lowered, EBOOK_EXTENSIONS) or _mentions(lowered, EBOOK_KEYWORDS)


def is_ebook_release(title: str) -> bool:
    """True when the release is an ebook and carries no sign of being audio."""
    return ebook_marker(title) is not None
