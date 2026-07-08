"""Helpers for parsing YouTube URLs and fetching transcripts."""

from contextlib import suppress
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api import NoTranscriptFound, YouTubeTranscriptApi, YouTubeTranscriptApiException

__all__ = ["Error", "InvalidUrlError", "TranscriptError", "fetch_transcript_from_url"]


class Error(Exception):
    """Base Exception for YouTube-related errors."""


class InvalidUrlError(Error):
    """Raised when a YouTube URL cannot be resolved to a video."""


class TranscriptError(Error):
    """Raised when a video transcript cannot be retrieved."""


def fetch_transcript_from_url(url: str) -> str | None:
    """Return transcript text for a YouTube URL.

    Returns:
        Transcript text for supported YouTube video URLs, or `None`
        for non-YouTube URLs.

    Raises:
        InvalidUrlError: Raised when the YouTube URL is malformed.
        TranscriptError: Raised when transcript retrieval fails.
    """

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    parts = [part for part in parsed.path.split("/") if part]
    video_id: str | None = None

    if host == "youtu.be":
        video_id = next(iter(parts), None)
    elif host not in {"m.youtube.com", "music.youtube.com", "youtube.com", "youtube-nocookie.com"}:
        return None
    elif parts[:1] == ["watch"]:
        video_id = parse_qs(parsed.query).get("v", [None])[0]
    elif len(parts) > 1 and parts[0] in {"shorts", "embed"}:
        video_id = parts[1]
    else:
        raise InvalidUrlError("Unsupported YouTube URL format.")

    if video_id is None:
        raise InvalidUrlError("Missing video id in YouTube URL.")

    try:
        transcripts = YouTubeTranscriptApi().list(video_id)
    except YouTubeTranscriptApiException as error:
        raise TranscriptError("Failed to load transcript metadata.") from error

    transcript = next(iter(transcripts), None)
    with suppress(NoTranscriptFound):
        transcript = transcripts.find_transcript(["en"])
    if transcript is None:
        raise TranscriptError("No transcript is available for this video.")

    try:
        snippets = transcript.fetch()
    except YouTubeTranscriptApiException as error:
        raise TranscriptError("Failed to fetch transcript contents.") from error

    lines = [text for snippet in snippets if (text := snippet.text.strip())]
    if not lines:
        raise TranscriptError("Transcript did not contain any text.")

    return "\n".join(lines)
