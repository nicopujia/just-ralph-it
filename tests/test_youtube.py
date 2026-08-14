import pytest

from jri.lib import youtube
from tests.doubles.youtube import FALLBACK_TRANSCRIPT, TRANSCRIPT, FakeApi


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123",
        "https://youtube.com/watch?v=abc123&t=30s",
        "https://music.youtube.com/watch?v=abc123",
        "https://m.youtube.com/watch?v=abc123",
        "https://youtu.be/abc123",
        "https://www.youtu.be/abc123",
        "https://www.youtube.com/shorts/abc123",
        "https://www.youtube-nocookie.com/embed/abc123",
    ],
    ids=["watch", "watch-extra-query", "music", "mobile", "short-link", "www-short-link", "shorts", "no-cookie-embed"],
)
def test_fetches_the_english_transcript_from_supported_urls(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    videos: list[str] = []
    monkeypatch.setattr(youtube, "YouTubeTranscriptApi", lambda: FakeApi(videos, []))

    # The double serves this transcript only down the English path, thus the result shows which path ran.
    assert youtube.fetch_transcript_from_url(url) == TRANSCRIPT
    # Each URL shape names the same video. The id the library asked for is what the parsing gives.
    assert videos == ["abc123"]


def test_falls_back_to_another_language_when_english_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "YouTubeTranscriptApi", lambda: FakeApi([], [], english=False))

    assert youtube.fetch_transcript_from_url("https://youtu.be/abc123") == FALLBACK_TRANSCRIPT


def test_reports_videos_with_transcripts_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "YouTubeTranscriptApi", lambda: FakeApi([], [], failure="list"))

    with pytest.raises(RuntimeError, match="Failed to load transcript metadata"):
        youtube.fetch_transcript_from_url("https://youtu.be/abc123")


def test_reports_videos_without_any_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "YouTubeTranscriptApi", lambda: FakeApi([], [], english=False, available=False))

    with pytest.raises(RuntimeError, match="No transcript is available"):
        youtube.fetch_transcript_from_url("https://youtu.be/abc123")


def test_reports_a_transcript_whose_contents_cannot_be_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "YouTubeTranscriptApi", lambda: FakeApi([], [], failure="fetch"))

    with pytest.raises(RuntimeError, match="Failed to fetch transcript contents"):
        youtube.fetch_transcript_from_url("https://youtu.be/abc123")


def test_reports_a_transcript_made_only_of_blank_snippets(monkeypatch: pytest.MonkeyPatch) -> None:
    # Auto-generated captions can list blank snippets for pauses or music. Without this check, JRI would hand the
    # model an empty transcript as if it held real content.
    monkeypatch.setattr(youtube, "YouTubeTranscriptApi", lambda: FakeApi([], [], ["", "   ", "\n\t"]))

    with pytest.raises(RuntimeError, match="did not contain any text"):
        youtube.fetch_transcript_from_url("https://youtu.be/abc123")


@pytest.mark.parametrize(
    "url",
    # `youtube.com.evil.test` carries `youtube.com` as a prefix. Matching the host by substring, rather than by
    # exact membership, would let an attacker-registered domain pass as YouTube.
    ["https://example.com/watch?v=abc123", "https://notyoutube.com/watch?v=abc123", "https://youtube.com.evil.test/"],
    ids=["other-site", "lookalike-host", "suffix-host"],
)
def test_ignores_urls_outside_youtube(url: str) -> None:
    assert youtube.fetch_transcript_from_url(url) is None


@pytest.mark.parametrize("url", ["youtube.com/watch?v=abc123", "youtu.be/abc123"], ids=["watch", "short-link"])
# `urlparse` needs `//` to read a host. Without a scheme, `youtube.com/watch` parses as a path with no host, so it
# looks the same as any other non-YouTube URL rather than raising.
def test_ignores_youtube_urls_without_a_scheme(url: str) -> None:
    assert youtube.fetch_transcript_from_url(url) is None


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://www.youtube.com/channel/abc123", "Unsupported"),
        ("https://www.youtube.com/", "Unsupported"),
        ("https://www.youtube.com/shorts/", "Unsupported"),
        ("https://www.youtube-nocookie.com/embed/", "Unsupported"),
        ("https://www.youtube.com/watch?list=abc123", "Missing video id"),
        ("https://youtu.be/", "Missing video id"),
    ],
    ids=["channel", "root", "shorts-without-id", "embed-without-id", "playlist", "short-link-without-id"],
)
def test_rejects_youtube_urls_without_a_video(url: str, reason: str) -> None:
    with pytest.raises(RuntimeError, match=reason):
        youtube.fetch_transcript_from_url(url)
