from pathlib import Path

import pytest

from jri.checks.schema import validate_repo
from jri.core.graph import check_graph_tree, validate_graph_tree


def _write_node(
    root: Path,
    semantic_path: str,
    *,
    title: str = "Node",
    state: str = "active",
    archive_reason: str | None = None,
    body: str = "Body\n",
) -> Path:
    node_path = root / ".jri" / "graph" / semantic_path / "NODE.md"
    node_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"title: {title}", f"state: {state}"]
    if archive_reason is not None:
        lines.append(f"archive_reason: {archive_reason}")
    lines.extend(["---", "", body])
    node_path.write_text("\n".join(lines), encoding="utf-8")
    return node_path


def test_check_graph_tree_allows_missing_or_empty_root(tmp_path: Path) -> None:
    missing = check_graph_tree(tmp_path)
    assert missing.active_count == 0
    assert missing.archived_count == 0
    assert missing.errors == ()

    (tmp_path / ".jri" / "graph").mkdir(parents=True)
    empty = check_graph_tree(tmp_path)
    assert empty.active_count == 0
    assert empty.archived_count == 0
    assert empty.errors == ()

    (tmp_path / ".jri" / "graph" / ".gitkeep").write_text("", encoding="utf-8")
    placeholder = check_graph_tree(tmp_path)
    assert placeholder.active_count == 0
    assert placeholder.archived_count == 0
    assert placeholder.errors == ()


def test_check_graph_tree_counts_valid_active_and_archived_nodes(
    tmp_path: Path,
) -> None:
    _write_node(tmp_path, "product", title="Product")
    _write_node(
        tmp_path,
        "product/old-checkout",
        title="Old checkout",
        state="archived",
        archive_reason="Replaced by new checkout",
    )

    result = check_graph_tree(tmp_path)

    assert result.active_count == 1
    assert result.archived_count == 1
    assert result.errors == ()
    validate_graph_tree(tmp_path)
    validate_repo(tmp_path)


def test_check_graph_tree_reports_malformed_yaml(tmp_path: Path) -> None:
    node_path = tmp_path / ".jri" / "graph" / "product" / "NODE.md"
    node_path.parent.mkdir(parents=True)
    node_path.write_text(
        "---\ntitle: [Product\nstate: active\n---\n\nBody\n",
        encoding="utf-8",
    )

    result = check_graph_tree(tmp_path)

    assert result.active_count == 0
    assert result.archived_count == 0
    assert result.errors == ("product/NODE.md: invalid node metadata YAML",)


def test_check_graph_tree_reports_unknown_frontmatter_keys(tmp_path: Path) -> None:
    node_path = _write_node(tmp_path, "product", title="Product")
    node_path.write_text(
        "---\ntitle: Product\nstate: active\nowner: team\n---\n\nBody\n",
        encoding="utf-8",
    )

    result = check_graph_tree(tmp_path)

    assert result.errors == ("product/NODE.md: node metadata has unknown keys: owner",)


def test_check_graph_tree_reports_invalid_state(tmp_path: Path) -> None:
    _write_node(tmp_path, "product", title="Product", state="draft")

    result = check_graph_tree(tmp_path)

    assert result.errors == (
        "product/NODE.md: node metadata state must be active or archived",
    )


def test_check_graph_tree_reports_missing_node_file_for_graph_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / ".jri" / "graph" / "product").mkdir(parents=True)

    result = check_graph_tree(tmp_path)

    assert result.errors == ("product: missing NODE.md",)


def test_check_graph_tree_reports_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    graph_dir = tmp_path / ".jri" / "graph"
    graph_dir.mkdir(parents=True)
    (graph_dir / "linked").symlink_to(outside, target_is_directory=True)

    result = check_graph_tree(tmp_path)

    assert result.errors == ("linked: symlink escapes .jri/graph",)


def test_check_graph_tree_reports_unexpected_files(tmp_path: Path) -> None:
    _write_node(tmp_path, "product", title="Product")
    (tmp_path / ".jri" / "graph" / "README.md").write_text("no\n", encoding="utf-8")
    (tmp_path / ".jri" / "graph" / "MANIFEST.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / ".jri" / "graph" / "product" / "extra.txt").write_text(
        "no\n",
        encoding="utf-8",
    )

    result = check_graph_tree(tmp_path)

    assert result.errors == (
        "README.md: unexpected file in graph root",
        "product/extra.txt: unexpected file in graph node directory",
    )


def test_check_graph_tree_reports_archived_node_missing_reason(tmp_path: Path) -> None:
    _write_node(tmp_path, "product", title="Product", state="archived")

    result = check_graph_tree(tmp_path)

    assert result.errors == (
        "product/NODE.md: archived node metadata requires non-empty archive_reason",
    )


def test_check_graph_tree_reports_malformed_directory_path(tmp_path: Path) -> None:
    bad_dir = tmp_path / ".jri" / "graph" / "bad\\path"
    bad_dir.mkdir(parents=True)
    (bad_dir / "NODE.md").write_text(
        "---\ntitle: Bad\nstate: active\n---\n\nBody\n",
        encoding="utf-8",
    )

    result = check_graph_tree(tmp_path)

    assert result.errors == ("bad\\path: graph path must be slash-separated with `/`",)


def test_validate_graph_tree_raises_deterministic_combined_errors(
    tmp_path: Path,
) -> None:
    (tmp_path / ".jri" / "graph" / "b").mkdir(parents=True)
    (tmp_path / ".jri" / "graph" / "a").mkdir(parents=True)

    with pytest.raises(ValueError) as exc_info:
        validate_graph_tree(tmp_path)

    assert str(exc_info.value) == "a: missing NODE.md; b: missing NODE.md"
