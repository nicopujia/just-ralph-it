"""Deterministic MVP readiness checks for finalized specs."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MvpReadinessReport:
    """Result of checking final specs for required readiness facts."""

    missing: tuple[str, ...]

    @property
    def is_ready(self) -> bool:
        """Return whether all required readiness buckets are present."""
        return not self.missing


@dataclass(frozen=True)
class _ReadinessBucket:
    label: str
    aliases: tuple[str, ...]


_READINESS_BUCKETS = (
    _ReadinessBucket("goal", ("goal", "project goal", "confirmed goal")),
    _ReadinessBucket(
        "target user",
        ("target user", "target users", "primary user", "users", "audience"),
    ),
    _ReadinessBucket(
        "workflows",
        ("workflow", "workflows", "user workflows", "core workflows"),
    ),
    _ReadinessBucket("inputs", ("input", "inputs", "user inputs")),
    _ReadinessBucket("outputs", ("output", "outputs", "user outputs")),
    _ReadinessBucket(
        "persistence",
        ("persistence", "storage", "saved data", "data storage"),
    ),
    _ReadinessBucket(
        "integrations",
        ("integration", "integrations", "external services", "apis"),
    ),
    _ReadinessBucket(
        "errors",
        ("error", "errors", "error handling", "failure modes"),
    ),
    _ReadinessBucket(
        "edge cases",
        ("edge case", "edge cases", "boundary cases"),
    ),
    _ReadinessBucket(
        "non-goals",
        ("non-goals", "non goals", "nongoals", "out of scope"),
    ),
    _ReadinessBucket(
        "success criteria",
        (
            "success criterion",
            "success criteria",
            "success metrics",
            "acceptance criteria",
        ),
    ),
)


def _normalize(value: str) -> str:
    normalized = value.strip().lower()
    normalized = normalized.replace("_", " ")
    normalized = re.sub(r"[`*_]+", "", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return normalized.strip()


_BUCKETS_BY_ALIAS = {
    _normalize(alias): bucket.label
    for bucket in _READINESS_BUCKETS
    for alias in bucket.aliases
}
_HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.+?)\s*$")
_LABEL_PATTERN = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?(?P<label>[A-Za-z][\w\s/-]+)"
    + r"(?:\*\*)?\s*:\s*(?P<body>.*)$"
)
_PLACEHOLDERS = {
    "tbd",
    "todo",
    "to do",
    "unknown",
    "pending",
    "not specified",
    "not decided",
    "unconfirmed",
}


def check_mvp_readiness(markdown: str) -> MvpReadinessReport:
    """Check whether Markdown specs include required MVP facts."""
    seen: set[str] = set()
    active_bucket: str | None = None

    for raw_line in markdown.splitlines():
        heading = _HEADING_PATTERN.match(raw_line)
        if heading:
            active_bucket = _record_heading(heading.group("text"), seen)
            continue

        label = _LABEL_PATTERN.match(raw_line)
        if label:
            bucket = _bucket_for(label.group("label"))
            if bucket is not None:
                active_bucket = bucket
                if _has_fact(label.group("body")):
                    seen.add(bucket)
                continue

        if active_bucket is not None and _has_fact(raw_line):
            seen.add(active_bucket)

    missing = tuple(
        bucket.label
        for bucket in _READINESS_BUCKETS
        if bucket.label not in seen
    )
    return MvpReadinessReport(missing=missing)


def format_missing_mvp_readiness(missing: tuple[str, ...]) -> str:
    """Format missing readiness facts in user-facing language."""
    lines = ["Missing MVP readiness facts:"]
    lines.extend(f"- {item}" for item in missing)
    lines.append("Please answer these before Ralph starts.")
    return "\n".join(lines)


def _record_heading(text: str, seen: set[str]) -> str | None:
    label, separator, body = text.partition(":")
    bucket = _bucket_for(label if separator else text)
    if bucket is not None and separator and _has_fact(body):
        seen.add(bucket)
    return bucket


def _bucket_for(label: str) -> str | None:
    return _BUCKETS_BY_ALIAS.get(_normalize(label))


def _has_fact(text: str) -> bool:
    normalized = _normalize(text)
    return bool(normalized) and normalized not in _PLACEHOLDERS
