from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import cast

import yaml

from .models import GRAPH_NODE_STATES, GraphNode, GraphNodeMetadata, GraphNodeState


def validate_graph_path(raw_path: str) -> str:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("graph path must be a non-empty string")
    if raw_path != raw_path.strip():
        raise ValueError("graph path must not contain leading or trailing whitespace")
    if raw_path.endswith("/") or "//" in raw_path:
        raise ValueError("graph path must not contain empty segments")
    if "\\" in raw_path:
        raise ValueError("graph path must be slash-separated with `/`")
    if PurePosixPath(raw_path).is_absolute() or PureWindowsPath(raw_path).is_absolute():
        raise ValueError("graph path must be relative")

    parts = raw_path.split("/")
    if any(part == "" for part in parts):
        raise ValueError("graph path must not contain empty segments")
    if any(part == ".." for part in parts):
        raise ValueError("graph path must not contain traversal segments")
    if any(part == "." for part in parts):
        raise ValueError("graph path must not contain current-directory segments")
    if any(part == "NODE.md" for part in parts):
        raise ValueError("graph path must not include raw NODE.md filenames")
    return "/".join(parts)


def graph_node_path(root: Path, semantic_path: str) -> Path:
    canonical_path = validate_graph_path(semantic_path)
    graph_dir = root.resolve(strict=False) / ".jri" / "graph"
    resolved_graph_dir = graph_dir.resolve(strict=False)
    if resolved_graph_dir != graph_dir:
        raise ValueError("refusing to write outside `.jri/graph/`")

    node_path = graph_dir.joinpath(*canonical_path.split("/"), "NODE.md")
    resolved_node_path = node_path.resolve(strict=False)
    try:
        resolved_node_path.relative_to(graph_dir)
    except ValueError as exc:
        raise ValueError("refusing to write outside `.jri/graph/`") from exc
    return node_path


def validate_node_metadata(payload: dict[str, object]) -> GraphNodeMetadata:
    allowed_keys = {"title", "state", "archive_reason"}
    unknown_keys = sorted(set(payload) - allowed_keys)
    if unknown_keys:
        joined = ", ".join(unknown_keys)
        raise ValueError(f"node metadata has unknown keys: {joined}")

    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("node metadata title must be a non-empty string")

    state = payload.get("state")
    if state not in GRAPH_NODE_STATES:
        raise ValueError("node metadata state must be active or archived")

    archive_reason = payload.get("archive_reason")
    if state == "archived":
        if not isinstance(archive_reason, str) or not archive_reason.strip():
            raise ValueError("archived node metadata requires non-empty archive_reason")
        return GraphNodeMetadata(
            title=title,
            state=cast(GraphNodeState, state),
            archive_reason=archive_reason,
        )

    if archive_reason is not None:
        if not isinstance(archive_reason, str):
            raise ValueError("node metadata archive_reason must be a string")
        if archive_reason.strip():
            raise ValueError("active node metadata must not include archive_reason")

    return GraphNodeMetadata(title=title, state=cast(GraphNodeState, state))


def parse_graph_node_file(root: Path, semantic_path: str) -> GraphNode:
    canonical_path = validate_graph_path(semantic_path)
    node_path = graph_node_path(root, canonical_path)
    text = node_path.read_text(encoding="utf-8")
    metadata_payload, body = _split_node_frontmatter(text)
    metadata = validate_node_metadata(metadata_payload)
    return GraphNode(
        path=node_path,
        semantic_path=canonical_path,
        metadata=metadata,
        body=body,
    )


def _split_node_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        raise ValueError("node file must start with YAML frontmatter")
    boundary = text.find("\n---\n", 4)
    if boundary == -1:
        raise ValueError("node file must end frontmatter with ---")

    metadata_text = text[4:boundary]
    body = text[boundary + len("\n---\n") :]
    if body.startswith("\n"):
        body = body[1:]

    try:
        loaded = yaml.safe_load(metadata_text)
    except yaml.YAMLError as exc:
        raise ValueError("invalid node metadata YAML") from exc
    if not isinstance(loaded, dict):
        raise ValueError("node frontmatter must be an object")
    return cast(dict[str, object], loaded), body
