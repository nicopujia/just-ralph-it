from collections.abc import Iterator
from typing import Any, NamedTuple, cast

from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled

TRANSCRIPT = "English\nlines"
FALLBACK_TRANSCRIPT = "Fallback\nlines"


class FakeSnippet(NamedTuple):
    text: str


class FakeTranscript:
    def __init__(self, language: str) -> None:
        self.language = language

    def fetch(self) -> list[FakeSnippet]:
        return [FakeSnippet(self.language), FakeSnippet("   "), FakeSnippet("lines")]


class FakeTranscripts:
    def __init__(self, languages: list[list[str]], *, english: bool, available: bool) -> None:
        self.languages = languages
        self.english = english
        self.available = available

    def __iter__(self) -> Iterator[FakeTranscript]:
        return iter([FakeTranscript("Fallback")] if self.available else [])

    def find_transcript(self, languages: list[str]) -> FakeTranscript:
        self.languages.append(languages)
        if not self.english:
            raise NoTranscriptFound("video", languages, cast("Any", None))
        return FakeTranscript("English")


class FakeApi:
    def __init__(
        self,
        videos: list[str],
        languages: list[list[str]],
        *,
        english: bool = True,
        available: bool = True,
        disabled: bool = False,
    ) -> None:
        self.videos = videos
        self.languages = languages
        self.english = english
        self.available = available
        self.disabled = disabled

    def list(self, video_id: str) -> FakeTranscripts:
        self.videos.append(video_id)
        if self.disabled:
            raise TranscriptsDisabled(video_id)
        return FakeTranscripts(self.languages, english=self.english, available=self.available)
