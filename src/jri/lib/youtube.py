"""Helpers for parsing YouTube URLs and fetching transcripts."""

from contextlib import suppress
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api import NoTranscriptFound, YouTubeTranscriptApi, YouTubeTranscriptApiException

__all__ = ["fetch_transcript_from_url"]


def fetch_transcript_from_url(url: str) -> str | None:
    """Return transcript text for a YouTube URL.

    Returns:
        Transcript text for supported YouTube video URLs, or `None`
        for non-YouTube URLs.

    Raises:
        RuntimeError: Raised when the URL is malformed or transcript
            retrieval fails.
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
        raise RuntimeError("Unsupported YouTube URL format.")

    if video_id is None:
        raise RuntimeError("Missing video id in YouTube URL.")

    try:
        transcripts = YouTubeTranscriptApi().list(video_id)
    except YouTubeTranscriptApiException as error:
        raise RuntimeError("Failed to load transcript metadata.") from error

    transcript = next(iter(transcripts), None)
    with suppress(NoTranscriptFound):
        transcript = transcripts.find_transcript(["en"])
    if transcript is None:
        raise RuntimeError("No transcript is available for this video.")

    try:
        snippets = transcript.fetch()
    except YouTubeTranscriptApiException as error:
        raise RuntimeError("Failed to fetch transcript contents.") from error

    lines = [text for snippet in snippets if (text := snippet.text.strip())]
    if not lines:
        raise RuntimeError("Transcript did not contain any text.")

    return "\n".join(lines)
