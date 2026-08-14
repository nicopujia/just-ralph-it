from collections.abc import Iterator
from typing import Any, Literal, NamedTuple, cast

from youtube_transcript_api import NoTranscriptFound, RequestBlocked, TranscriptsDisabled

type FakeFailure = Literal["list", "fetch"]

TRANSCRIPT = "English\nlines"
FALLBACK_TRANSCRIPT = "Fallback\nlines"


class FakeSnippet(NamedTuple):
    text: str


class FakeTranscript:
    def __init__(self, language: str, texts: list[str] | None = None, *, fetchable: bool = True) -> None:
        self.language = language
        self.texts = texts
        self.fetchable = fetchable

    def fetch(self) -> list[FakeSnippet]:
        if not self.fetchable:
            raise RequestBlocked("video")
        texts = [self.language, "   ", "lines"] if self.texts is None else self.texts
        return [FakeSnippet(text) for text in texts]


class FakeTranscripts:
    def __init__(
        self, texts: list[str] | None = None, *, english: bool, available: bool, fetchable: bool = True
    ) -> None:
        self.texts = texts
        self.english = english
        self.available = available
        self.fetchable = fetchable

    def __iter__(self) -> Iterator[FakeTranscript]:
        transcripts = [FakeTranscript("Fallback", self.texts, fetchable=self.fetchable)] if self.available else []
        return iter(transcripts)

    def find_transcript(self, languages: list[str]) -> FakeTranscript:
        if not self.english:
            raise NoTranscriptFound("video", languages, cast("Any", None))
        return FakeTranscript("English", self.texts, fetchable=self.fetchable)


class FakeApi:
    def __init__(
        self,
        videos: list[str],
        texts: list[str] | None = None,
        *,
        english: bool = True,
        available: bool = True,
        failure: "FakeFailure | None" = None,
    ) -> None:
        self.videos = videos
        self.texts = texts
        self.english = english
        self.available = available
        self.failure = failure

    def list(self, video_id: str) -> FakeTranscripts:
        self.videos.append(video_id)
        if self.failure == "list":
            raise TranscriptsDisabled(video_id)
        return FakeTranscripts(
            self.texts, english=self.english, available=self.available, fetchable=self.failure != "fetch"
        )
