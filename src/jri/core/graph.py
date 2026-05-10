import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import cast

import yaml

from .models import GRAPH_NODE_STATES, GraphNode, GraphNodeMetadata, GraphNodeState


@dataclass(frozen=True)
class GraphChildSummary:
    semantic_path: str
    title: str
    state: GraphNodeState


@dataclass(frozen=True)
class GraphNodeRead:
    path: Path
    semantic_path: str
    metadata: GraphNodeMetadata
    body: str
    children: tuple[GraphChildSummary, ...]


@dataclass(frozen=True)
class GraphPatchNodeSummary:
    path: str
    additions: int
    deletions: int


@dataclass(frozen=True)
class GraphPatchSummary:
    nodes: tuple[GraphPatchNodeSummary, ...]


@dataclass(frozen=True)
class GraphCheckResult:
    active_count: int
    archived_count: int
    errors: tuple[str, ...]

    @property
    def malformed_count(self) -> int:
        return len(self.errors)


@dataclass(frozen=True)
class _GraphPatchHunk:
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    additions: int
    deletions: int


@dataclass(frozen=True)
class _GraphPatchOperation:
    path: str
    hunks: tuple[_GraphPatchHunk, ...]


class GraphStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def create_node(self, path: str, title: str, body: str) -> GraphNode:
        canonical_path = validate_graph_path(path)
        metadata = validate_node_metadata({"title": title, "state": "active"})
        node_path = graph_node_path(self.root, canonical_path)
        if node_path.exists():
            raise ValueError(f"graph node `{canonical_path}` already exists")

        self._create_missing_parent_nodes(canonical_path)
        node_path.parent.mkdir(parents=True, exist_ok=True)
        node = GraphNode(
            path=node_path,
            semantic_path=canonical_path,
            metadata=metadata,
            body=body,
        )
        self.write_node(node)
        return node

    def read_node(self, path: str, depth: int = 1) -> GraphNodeRead:
        if depth < 0:
            raise ValueError("read depth must be non-negative")
        node = self.read_existing_node(path)
        children = tuple(self._child_summaries(node.semantic_path, depth))
        return GraphNodeRead(
            path=node.path,
            semantic_path=node.semantic_path,
            metadata=node.metadata,
            body=node.body,
            children=children,
        )

    def update_node_metadata(
        self,
        path: str,
        *,
        title: str | None = None,
        state: GraphNodeState | None = None,
        archive_reason: str | None = None,
    ) -> GraphNode:
        node = self.read_existing_node(path)
        next_state = state if state is not None else node.metadata.state
        payload: dict[str, object] = {
            "title": title if title is not None else node.metadata.title,
            "state": next_state,
        }
        if archive_reason is not None:
            payload["archive_reason"] = archive_reason
        elif next_state == "archived" and node.metadata.archive_reason is not None:
            payload["archive_reason"] = node.metadata.archive_reason

        metadata = validate_node_metadata(payload)
        updated = GraphNode(
            path=node.path,
            semantic_path=node.semantic_path,
            metadata=metadata,
            body=node.body,
        )
        self.write_node(updated)
        return updated

    def move_node(self, source: str, destination: str) -> GraphNode:
        if not source.strip():
            raise ValueError("cannot move graph root")
        source_path = validate_graph_path(source)
        destination_path = validate_graph_path(destination)
        source_node_path = graph_node_path(self.root, source_path)
        destination_node_path = graph_node_path(self.root, destination_path)
        source_dir = source_node_path.parent
        destination_dir = destination_node_path.parent

        if not source_node_path.exists():
            raise FileNotFoundError(f"graph node `{source_path}` not found")
        subtree_prefix = f"{source_path}/"
        if destination_path != source_path and destination_path.startswith(
            subtree_prefix
        ):
            raise ValueError("cannot move graph node into its own subtree")
        if destination_node_path.exists() or destination_dir.exists():
            raise ValueError(f"graph node `{destination_path}` already exists")

        created_parent_paths = self._create_missing_parent_nodes(destination_path)
        try:
            destination_dir.parent.mkdir(parents=True, exist_ok=True)
            source_dir.replace(destination_dir)
        except Exception:
            self._remove_created_parent_nodes(created_parent_paths)
            raise
        return self.read_existing_node(destination_path)

    def read_existing_node(self, path: str) -> GraphNode:
        canonical_path = validate_graph_path(path)
        node_path = graph_node_path(self.root, canonical_path)
        if not node_path.exists():
            raise FileNotFoundError(f"graph node `{canonical_path}` not found")
        return parse_graph_node_file(self.root, canonical_path)

    def _create_missing_parent_nodes(self, semantic_path: str) -> tuple[str, ...]:
        parts = validate_graph_path(semantic_path).split("/")[:-1]
        created: list[str] = []
        for index in range(1, len(parts) + 1):
            parent_path = "/".join(parts[:index])
            node_path = graph_node_path(self.root, parent_path)
            if node_path.exists():
                continue
            node_path.parent.mkdir(parents=True, exist_ok=True)
            node = GraphNode(
                path=node_path,
                semantic_path=parent_path,
                metadata=GraphNodeMetadata(
                    title=_title_from_segment(parts[index - 1]), state="active"
                ),
                body="",
            )
            self.write_node(node)
            created.append(parent_path)
        return tuple(created)

    def _remove_created_parent_nodes(self, semantic_paths: tuple[str, ...]) -> None:
        for semantic_path in reversed(semantic_paths):
            node_path = graph_node_path(self.root, semantic_path)
            parent_dir = node_path.parent
            if parent_dir.exists():
                shutil.rmtree(parent_dir)

    def _child_summaries(self, parent_path: str, depth: int) -> list[GraphChildSummary]:
        if depth == 0:
            return []
        parent_node_path = graph_node_path(self.root, parent_path)
        parent_dir = parent_node_path.parent
        if not parent_dir.exists():
            return []

        summaries: list[GraphChildSummary] = []
        for child_dir in sorted(
            (item for item in parent_dir.iterdir() if item.is_dir()),
            key=lambda item: item.name,
        ):
            child_semantic_path = f"{parent_path}/{child_dir.name}"
            child_node_path = graph_node_path(self.root, child_semantic_path)
            if not child_node_path.exists():
                continue
            child = parse_graph_node_file(self.root, child_semantic_path)
            summaries.append(
                GraphChildSummary(
                    semantic_path=child.semantic_path,
                    title=child.metadata.title,
                    state=child.metadata.state,
                )
            )
            if child.metadata.state == "active":
                summaries.extend(self._child_summaries(child.semantic_path, depth - 1))
        return summaries

    def write_node(self, node: GraphNode) -> None:
        node.path.parent.mkdir(parents=True, exist_ok=True)
        text = dump_graph_node(node)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{node.path.name}.", suffix=".tmp", dir=node.path.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, node.path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        _fsync_directory(node.path.parent)


def apply_graph_patch(store: GraphStore, patch_text: str) -> GraphPatchSummary:
    operations = _parse_graph_patch(patch_text)
    planned: list[tuple[GraphNode, str, int, int]] = []

    for operation in operations:
        node = store.read_existing_node(operation.path)
        next_body, additions, deletions = _apply_graph_patch_hunks(
            node.body, node.semantic_path, operation.hunks
        )
        planned.append((node, next_body, additions, deletions))

    if all(node.body == next_body for node, next_body, _, _ in planned):
        raise ValueError("graph patch is a no-op")

    summaries: list[GraphPatchNodeSummary] = []
    written_originals: list[GraphNode] = []
    try:
        for node, next_body, additions, deletions in planned:
            updated = GraphNode(
                path=node.path,
                semantic_path=node.semantic_path,
                metadata=node.metadata,
                body=next_body,
            )
            store.write_node(updated)
            written_originals.append(node)
            summaries.append(
                GraphPatchNodeSummary(
                    path=node.semantic_path, additions=additions, deletions=deletions
                )
            )
    except Exception:
        for original in reversed(written_originals):
            store.write_node(original)
        raise

    return GraphPatchSummary(nodes=tuple(summaries))


def _parse_graph_patch(patch_text: str) -> tuple[_GraphPatchOperation, ...]:
    if not patch_text.strip():
        raise ValueError("empty patch")

    normalized = patch_text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    lines = normalized.split("\n")
    if lines[0] != "*** Begin Graph Patch" or lines[-1] != "*** End Graph Patch":
        raise ValueError(
            "graph patch must start with `*** Begin Graph Patch` and end with "
            "`*** End Graph Patch`"
        )
    if len(lines) == 2:
        raise ValueError("empty patch")

    operations: list[_GraphPatchOperation] = []
    index = 1
    while index < len(lines) - 1:
        line = lines[index]
        if line.startswith("*** Move to:"):
            raise ValueError("graph patch does not support node move operations")
        if line.startswith("*** ") and not line.startswith("*** Update Node: "):
            raise ValueError("unsupported graph patch operation")
        if not line.startswith("*** Update Node: "):
            raise ValueError(
                "graph patch operation must be `*** Update Node: <semantic-path>`"
            )

        raw_path = line.removeprefix("*** Update Node: ")
        if raw_path != raw_path.strip() or raw_path == "":
            raise ValueError("graph patch node path is malformed")
        path = validate_graph_path(raw_path)
        index += 1

        hunks: list[_GraphPatchHunk] = []
        while index < len(lines) - 1 and not lines[index].startswith(
            "*** Update Node: "
        ):
            if lines[index].startswith("*** Move to:"):
                raise ValueError("graph patch does not support node move operations")
            if lines[index].startswith("*** "):
                raise ValueError("unsupported graph patch operation")
            if not lines[index].startswith("@@"):
                raise ValueError("graph patch update requires hunks starting with `@@`")
            index += 1

            old_lines: list[str] = []
            new_lines: list[str] = []
            additions = 0
            deletions = 0
            while index < len(lines) - 1:
                change_line = lines[index]
                if change_line.startswith("@@") or change_line.startswith(
                    "*** Update Node: "
                ):
                    break
                if change_line.startswith("*** Move to:"):
                    raise ValueError(
                        "graph patch does not support node move operations"
                    )
                if change_line.startswith("*** "):
                    raise ValueError("unsupported graph patch operation")
                if not change_line or change_line[0] not in {" ", "-", "+"}:
                    raise ValueError(
                        "graph patch hunk lines must start with space, `-`, or `+`"
                    )

                content = change_line[1:]
                if content == "---":
                    raise ValueError("graph patch cannot edit node frontmatter")
                if change_line[0] == " ":
                    old_lines.append(content)
                    new_lines.append(content)
                elif change_line[0] == "-":
                    old_lines.append(content)
                    deletions += 1
                else:
                    new_lines.append(content)
                    additions += 1
                index += 1

            hunks.append(
                _GraphPatchHunk(
                    old_lines=tuple(old_lines),
                    new_lines=tuple(new_lines),
                    additions=additions,
                    deletions=deletions,
                )
            )

        if not hunks:
            raise ValueError("graph patch update requires at least one hunk")
        operations.append(_GraphPatchOperation(path=path, hunks=tuple(hunks)))

    if not operations:
        raise ValueError("empty patch")
    return tuple(operations)


def _apply_graph_patch_hunks(
    body: str, path: str, hunks: tuple[_GraphPatchHunk, ...]
) -> tuple[str, int, int]:
    body_lines = _graph_body_to_lines(body)
    replacements: list[tuple[int, int, tuple[str, ...]]] = []
    additions = 0
    deletions = 0
    search_start = 0

    for hunk in hunks:
        additions += hunk.additions
        deletions += hunk.deletions

        if not hunk.old_lines:
            replacements.append((len(body_lines), 0, hunk.new_lines))
            continue

        match_at = _find_line_sequence(body_lines, hunk.old_lines, search_start)
        if match_at == -1:
            expected = "\n".join(hunk.old_lines)
            raise ValueError(
                f"failed to find expected lines in graph node `{path}`:\n{expected}"
            )
        replacements.append((match_at, len(hunk.old_lines), hunk.new_lines))
        search_start = match_at + len(hunk.old_lines)

    next_lines = list(body_lines)
    for start, old_count, new_lines in reversed(replacements):
        next_lines[start : start + old_count] = new_lines
    return _graph_lines_to_body(next_lines), additions, deletions


def _graph_body_to_lines(body: str) -> list[str]:
    if body == "":
        return []
    if body.endswith("\n"):
        return body[:-1].split("\n") if body[:-1] else []
    return body.split("\n")


def _graph_lines_to_body(lines: list[str]) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _find_line_sequence(lines: list[str], sequence: tuple[str, ...], start: int) -> int:
    if not sequence:
        return -1
    for index in range(start, len(lines) - len(sequence) + 1):
        if lines[index : index + len(sequence)] == list(sequence):
            return index
    return -1


def dump_graph_node(node: GraphNode) -> str:
    payload = {"title": node.metadata.title, "state": node.metadata.state}
    if node.metadata.archive_reason is not None:
        payload["archive_reason"] = node.metadata.archive_reason
    frontmatter = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False).strip()
    return "---\n" + frontmatter + "\n---\n\n" + node.body


def _title_from_segment(segment: str) -> str:
    return segment.replace("-", " ").replace("_", " ").title()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_graph_path(raw_path: str) -> str:
    if not raw_path.strip():
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


def check_graph_tree(root: Path) -> GraphCheckResult:
    graph_dir = root.resolve(strict=False) / ".jri" / "graph"
    if not graph_dir.exists():
        return GraphCheckResult(active_count=0, archived_count=0, errors=())
    if graph_dir.is_symlink():
        return GraphCheckResult(
            active_count=0,
            archived_count=0,
            errors=(".jri/graph: symlink escapes .jri/graph",),
        )
    if not graph_dir.is_dir():
        return GraphCheckResult(
            active_count=0,
            archived_count=0,
            errors=(".jri/graph: expected directory",),
        )

    active_count = 0
    archived_count = 0
    errors: list[str] = []
    pending = [graph_dir]

    while pending:
        directory = pending.pop()
        relative_directory = directory.relative_to(graph_dir)
        semantic_path = _graph_relative_path(relative_directory)
        path_is_valid = True
        if semantic_path:
            try:
                validate_graph_path(semantic_path)
            except ValueError as exc:
                path_is_valid = False
                errors.append(f"{semantic_path}: {exc}")

        entries = sorted(directory.iterdir(), key=lambda item: item.name)
        if semantic_path and path_is_valid:
            node_path = directory / "NODE.md"
            if not node_path.exists():
                errors.append(f"{semantic_path}: missing NODE.md")
            elif node_path.is_symlink():
                if _path_escapes(node_path, graph_dir):
                    errors.append(
                        f"{semantic_path}/NODE.md: symlink escapes .jri/graph"
                    )
                else:
                    errors.append(
                        f"{semantic_path}/NODE.md: "
                        "symlinked graph entries are unsupported"
                    )
            elif node_path.is_file():
                try:
                    node = parse_graph_node_file(root, semantic_path)
                except ValueError as exc:
                    errors.append(f"{semantic_path}/NODE.md: {exc}")
                else:
                    if node.metadata.state == "active":
                        active_count += 1
                    else:
                        archived_count += 1

        for entry in entries:
            entry_relative = _graph_relative_path(entry.relative_to(graph_dir))
            if entry.is_symlink():
                if semantic_path and entry.name == "NODE.md":
                    continue
                if _path_escapes(entry, graph_dir):
                    errors.append(f"{entry_relative}: symlink escapes .jri/graph")
                else:
                    errors.append(
                        f"{entry_relative}: symlinked graph entries are unsupported"
                    )
                continue
            if entry.is_dir():
                pending.append(entry)
                continue
            if semantic_path:
                if entry.name != "NODE.md":
                    errors.append(
                        f"{entry_relative}: unexpected file in graph node directory"
                    )
            elif entry.name != "MANIFEST.json":
                errors.append(f"{entry_relative}: unexpected file in graph root")

    return GraphCheckResult(
        active_count=active_count,
        archived_count=archived_count,
        errors=tuple(sorted(errors)),
    )


def validate_graph_tree(root: Path) -> None:
    result = check_graph_tree(root)
    if result.errors:
        raise ValueError("; ".join(result.errors))


def _graph_relative_path(path: Path) -> str:
    if str(path) == ".":
        return ""
    return path.as_posix()


def _path_escapes(path: Path, graph_dir: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(graph_dir)
    except ValueError:
        return True
    return False
