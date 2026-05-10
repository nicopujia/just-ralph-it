import json
from pathlib import Path
from typing import cast

from .....graph import (
    GraphStore,
    apply_graph_patch,
    graph_node_path,
    validate_graph_path,
)
from .....models import GraphNodeMetadata, GraphNodeState
from ._validation import service


def run_compile_graph(payload: dict[str, object]) -> str:
    if payload:
        raise ValueError("compile-graph does not accept arguments")
    return _json(service(Path.cwd()).compile_graph())


def run_create_node(payload: dict[str, object]) -> str:
    path = _required_graph_path(payload, "path")
    title = _required_string(payload, "title")
    body = _optional_string(payload, "body") or ""

    store = GraphStore(Path.cwd())
    auto_created_parents = _missing_parent_paths(store.root, path)
    node = store.create_node(path, title, body)
    return _json(
        {
            "path": node.semantic_path,
            "auto_created_parents": auto_created_parents,
        }
    )


def run_list_nodes(payload: dict[str, object]) -> str:
    if payload:
        raise ValueError("list-nodes does not accept arguments")
    nodes = GraphStore(Path.cwd()).list_nodes()
    return _json(
        {
            "nodes": [
                {
                    "path": node.semantic_path,
                    "title": node.title,
                    "state": node.state,
                }
                for node in nodes
            ]
        }
    )


def run_read_node(payload: dict[str, object]) -> str:
    path = _required_graph_path(payload, "path")
    depth = _optional_non_negative_int(payload, "depth", default=1)

    node = GraphStore(Path.cwd()).read_node(path, depth=depth)
    return _json(
        {
            "path": node.semantic_path,
            "metadata": _metadata_payload(node.metadata),
            "body": node.body,
            "children": [
                {
                    "path": child.semantic_path,
                    "title": child.title,
                    "state": child.state,
                }
                for child in node.children
            ],
        }
    )


def run_search_nodes(payload: dict[str, object]) -> str:
    query = _required_string(payload, "query")
    limit = min(_optional_non_negative_int(payload, "limit", default=10), 50)
    include_archived = _optional_bool(payload, "include_archived", default=False)

    matches = GraphStore(Path.cwd()).search_nodes(
        query, limit=limit, include_archived=include_archived
    )
    return _json(
        {
            "query": query,
            "matches": [
                {
                    "path": match.semantic_path,
                    "title": match.title,
                    "state": match.state,
                    "score": match.score,
                    "snippet": match.snippet,
                }
                for match in matches
            ],
        }
    )


def run_apply_graph_patch(payload: dict[str, object]) -> str:
    patch = _required_string(payload, "patch")
    summary = apply_graph_patch(GraphStore(Path.cwd()), patch)
    return _json(
        {
            "changed_nodes": [
                {
                    "path": node.path,
                    "additions": node.additions,
                    "deletions": node.deletions,
                }
                for node in summary.nodes
            ]
        }
    )


def run_update_node_metadata(payload: dict[str, object]) -> str:
    path = _required_graph_path(payload, "path")
    title = _optional_string(payload, "title")
    state = _optional_graph_state(payload, "state")
    archive_reason = _optional_string(payload, "archive_reason")

    if title is None and state is None and archive_reason is None:
        raise ValueError("at least one metadata field must be provided")

    node = GraphStore(Path.cwd()).update_node_metadata(
        path,
        title=title,
        state=state,
        archive_reason=archive_reason,
    )
    return _json(
        {"path": node.semantic_path, "metadata": _metadata_payload(node.metadata)}
    )


def run_move_node(payload: dict[str, object]) -> str:
    source_path = _required_graph_path(payload, "source_path")
    destination_path = _required_graph_path(payload, "destination_path")

    store = GraphStore(Path.cwd())
    moved_subtree_count = _subtree_node_count(store.root, source_path)
    node = store.move_node(source_path, destination_path)
    return _json(
        {
            "old_path": source_path,
            "new_path": node.semantic_path,
            "moved_subtree_count": moved_subtree_count,
        }
    )


def _required_graph_path(payload: dict[str, object], name: str) -> str:
    return validate_graph_path(_required_string(payload, name))


def _required_string(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{name}` must be a non-empty string")
    return value


def _optional_string(
    payload: dict[str, object], name: str, *, default: str | None = None
) -> str | None:
    value = payload.get(name, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"`{name}` must be a string")
    return value


def _optional_bool(payload: dict[str, object], name: str, *, default: bool) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"`{name}` must be a boolean")
    return value


def _optional_graph_state(
    payload: dict[str, object], name: str
) -> GraphNodeState | None:
    value = payload.get(name)
    if value is None:
        return None
    if value not in {"active", "archived"}:
        raise ValueError(f"`{name}` must be active or archived")
    return cast(GraphNodeState, value)


def _optional_non_negative_int(
    payload: dict[str, object], name: str, *, default: int
) -> int:
    value = payload.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"`{name}` must be a non-negative integer")
    return value


def _missing_parent_paths(root: Path, semantic_path: str) -> list[str]:
    parts = validate_graph_path(semantic_path).split("/")[:-1]
    missing: list[str] = []
    for index in range(1, len(parts) + 1):
        parent_path = "/".join(parts[:index])
        if not graph_node_path(root, parent_path).exists():
            missing.append(parent_path)
    return missing


def _subtree_node_count(root: Path, semantic_path: str) -> int:
    node_path = graph_node_path(root, semantic_path)
    if not node_path.exists():
        return 0
    return sum(1 for item in node_path.parent.rglob("NODE.md") if item.is_file())


def _metadata_payload(metadata: GraphNodeMetadata) -> dict[str, object]:
    payload: dict[str, object] = {"title": metadata.title, "state": metadata.state}
    if metadata.archive_reason is not None:
        payload["archive_reason"] = metadata.archive_reason
    return payload


def _json(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2) + "\n"
