"""Helpers for parsing YouTube URLs and fetching transcripts."""

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
    """

    if (video_id := _get_video_id(url)) is None:
        return None
    return _fetch_transcript(video_id)


def _get_video_id(url: str) -> str | None:
    """Extract a video id from a supported URL.

    Returns:
        The resolved video id for supported video URLs, or `None`
        for non-YouTube URLs.

    Raises:
        InvalidUrlError: The URL is a YouTube URL but does not point
            to a supported video.
    """

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    parts = [part for part in parsed.path.split("/") if part]

    if host == "youtu.be":
        if parts:
            return parts[0]
        raise InvalidUrlError("Missing video id in YouTube URL.")

    if host not in {"m.youtube.com", "music.youtube.com", "youtube.com", "youtube-nocookie.com"}:
        return None

    if parts[:1] == ["watch"]:
        if video_id := parse_qs(parsed.query).get("v", [None])[0]:
            return video_id
        raise InvalidUrlError("Missing video id in YouTube URL.")
    if len(parts) > 1 and parts[0] in {"shorts", "embed"}:
        return parts[1]
    raise InvalidUrlError("Unsupported YouTube URL format.")


def _fetch_transcript(video_id: str) -> str:
    """Fetch and normalize transcript text.

    Returns:
        Joined transcript text with blank snippets removed.

    Raises:
        TranscriptError: The transcript could not be loaded.
    """

    try:
        transcripts = YouTubeTranscriptApi().list(video_id)
    except YouTubeTranscriptApiException as error:
        raise TranscriptError("Failed to load transcript metadata.") from error

    try:
        transcript = transcripts.find_transcript(["en"])
    except NoTranscriptFound as error:
        transcript = next(iter(transcripts), None)
        if transcript is None:
            raise TranscriptError("No transcript is available for this video.") from error

    try:
        snippets = transcript.fetch()
    except YouTubeTranscriptApiException as error:
        raise TranscriptError("Failed to fetch transcript contents.") from error

    lines = [text for snippet in snippets if (text := snippet.text.strip())]
    if not lines:
        raise TranscriptError("Transcript did not contain any text.")

    return "\n".join(lines)
