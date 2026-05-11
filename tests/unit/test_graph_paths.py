from pathlib import Path

import pytest

from jri.core.graph import (
    graph_node_path,
    parse_graph_node_file,
    validate_graph_path,
    validate_node_metadata,
)
from jri.core.models import GraphNodeMetadata
from jri.core.paths import JriPaths


def test_graph_path_maps_to_node_file_under_graph_dir(tmp_path: Path) -> None:
    path = graph_node_path(tmp_path, "product/checkout")

    assert path == tmp_path / ".jri" / "graph" / "product" / "checkout" / "NODE.md"
    assert validate_graph_path("product/checkout") == "product/checkout"
    assert JriPaths(tmp_path).graph_node_path("product/checkout") == path


@pytest.mark.parametrize(
    ("raw_path", "expected_message"),
    [
        ("", "non-empty"),
        (" product", "leading or trailing whitespace"),
        ("/absolute", "relative"),
        ("C:/absolute", "relative"),
        (r"auth\oauth", "slash-separated"),
        ("product//checkout", "empty segments"),
        ("product/../checkout", "traversal"),
        ("product/./checkout", "current-directory"),
        ("product/NODE.md", "NODE.md"),
        ("NODE.md", "NODE.md"),
    ],
)
def test_graph_path_rejects_unsafe_or_non_semantic_inputs(
    raw_path: str, expected_message: str
) -> None:
    with pytest.raises(ValueError, match=expected_message):
        validate_graph_path(raw_path)


def test_graph_node_path_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    graph_dir = tmp_path / ".jri" / "graph"
    graph_dir.mkdir(parents=True)
    (graph_dir / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="outside `.jri/graph/`"):
        graph_node_path(tmp_path, "linked/child")


def test_graph_node_path_rejects_symlinked_graph_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside-graph"
    outside.mkdir()
    (tmp_path / ".jri").mkdir()
    (tmp_path / ".jri" / "graph").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="outside `.jri/graph/`"):
        graph_node_path(tmp_path, "product/checkout")


def test_node_metadata_accepts_active_and_archived_states() -> None:
    assert validate_node_metadata({"title": "Checkout", "state": "active"}) == (
        GraphNodeMetadata(title="Checkout", state="active")
    )
    assert validate_node_metadata(
        {"title": "Old checkout", "state": "archived", "archive_reason": "Replaced"}
    ) == GraphNodeMetadata(
        title="Old checkout", state="archived", archive_reason="Replaced"
    )
    assert validate_node_metadata(
        {"title": "Checkout", "state": "active", "archive_reason": ""}
    ) == GraphNodeMetadata(title="Checkout", state="active")


def test_node_metadata_rejects_non_string_archive_reason_for_active_node() -> None:
    with pytest.raises(ValueError, match="archive_reason"):
        validate_node_metadata(
            {"title": "Checkout", "state": "active", "archive_reason": 12}
        )


@pytest.mark.parametrize(
    ("payload", "expected_message"),
    [
        ({"state": "active"}, "title"),
        ({"title": 12, "state": "active"}, "title"),
        ({"title": "Checkout", "state": "draft"}, "state"),
        ({"title": "Checkout", "state": "active", "extra": True}, "unknown"),
        ({"title": "Checkout", "state": "archived"}, "archive_reason"),
        (
            {"title": "Checkout", "state": "archived", "archive_reason": ""},
            "archive_reason",
        ),
        (
            {"title": "Checkout", "state": "active", "archive_reason": "Replaced"},
            "active",
        ),
        (
            {"title": "Checkout", "state": "archived", "archive_reason": 12},
            "archive_reason",
        ),
    ],
)
def test_node_metadata_rejects_invalid_payloads(
    payload: dict[str, object], expected_message: str
) -> None:
    with pytest.raises(ValueError, match=expected_message):
        validate_node_metadata(payload)


def test_parse_graph_node_file_reads_frontmatter_and_body(tmp_path: Path) -> None:
    node_file = graph_node_path(tmp_path, "product/checkout")
    node_file.parent.mkdir(parents=True)
    node_file.write_text(
        "---\ntitle: Checkout\nstate: active\n---\n\nOwns checkout intent.\n",
        encoding="utf-8",
    )

    node = parse_graph_node_file(tmp_path, "product/checkout")

    assert node.path == node_file
    assert node.semantic_path == "product/checkout"
    assert node.metadata == GraphNodeMetadata(title="Checkout", state="active")
    assert node.body == "Owns checkout intent.\n"


def test_parse_graph_node_file_rejects_raw_node_md_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="NODE.md"):
        parse_graph_node_file(tmp_path, "product/checkout/NODE.md")


def test_parse_graph_node_file_rejects_missing_frontmatter(tmp_path: Path) -> None:
    node_file = graph_node_path(tmp_path, "product/checkout")
    node_file.parent.mkdir(parents=True)
    node_file.write_text("Owns checkout intent.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="start with YAML frontmatter"):
        parse_graph_node_file(tmp_path, "product/checkout")


def test_parse_graph_node_file_rejects_missing_frontmatter_boundary(
    tmp_path: Path,
) -> None:
    node_file = graph_node_path(tmp_path, "product/checkout")
    node_file.parent.mkdir(parents=True)
    node_file.write_text(
        "---\ntitle: Checkout\nstate: active\n\nOwns checkout.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="end frontmatter"):
        parse_graph_node_file(tmp_path, "product/checkout")


def test_parse_graph_node_file_rejects_non_object_frontmatter(tmp_path: Path) -> None:
    node_file = graph_node_path(tmp_path, "product/checkout")
    node_file.parent.mkdir(parents=True)
    node_file.write_text("---\n- checkout\n---\n\nOwns checkout.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="frontmatter must be an object"):
        parse_graph_node_file(tmp_path, "product/checkout")


def test_parse_graph_node_file_wraps_invalid_yaml(tmp_path: Path) -> None:
    node_file = graph_node_path(tmp_path, "product/checkout")
    node_file.parent.mkdir(parents=True)
    node_file.write_text(
        "---\ntitle: [Checkout\nstate: active\n---\n\nOwns checkout intent.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid node metadata YAML"):
        parse_graph_node_file(tmp_path, "product/checkout")
