import logging
from contextlib import suppress
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api import NoTranscriptFound, YouTubeTranscriptApi, YouTubeTranscriptApiException

__all__ = ["fetch_transcript_from_url"]

logger = logging.getLogger(__name__)


def fetch_transcript_from_url(url: str) -> str | None:
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
        logger.exception("transcripts_load_failed url=%r video_id=%r", url, video_id)
        raise RuntimeError("Failed to load transcript metadata.") from error

    transcript = next(iter(transcripts), None)
    with suppress(NoTranscriptFound):
        transcript = transcripts.find_transcript(["en"])
    if transcript is None:
        raise RuntimeError("No transcript is available for this video.")

    try:
        snippets = transcript.fetch()
    except YouTubeTranscriptApiException as error:
        logger.exception("transcript_fetch_failed url=%r video_id=%r", url, video_id)
        raise RuntimeError("Failed to fetch transcript contents.") from error

    lines = [text for snippet in snippets if (text := snippet.text.strip())]
    if not lines:
        raise RuntimeError("Transcript did not contain any text.")

    logger.info("transcript_fetched video_id=%s characters=%d", video_id, len(output := "\n".join(lines)))
    return output
