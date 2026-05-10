from pathlib import Path
from typing import Any

import pytest

from jri.core.graph import GraphStore, parse_graph_node_file
from jri.core.models import GraphNodeMetadata


def test_create_node_creates_parents(
    tmp_path: Path,
) -> None:
    store = GraphStore(tmp_path)

    node = store.create_node("product/checkout/payment", "Payment", "Collect money.\n")

    assert node.semantic_path == "product/checkout/payment"
    assert node.metadata == GraphNodeMetadata(title="Payment", state="active")
    assert node.body == "Collect money.\n"
    assert parse_graph_node_file(tmp_path, "product").metadata == GraphNodeMetadata(
        title="Product", state="active"
    )
    assert parse_graph_node_file(tmp_path, "product").body == ""
    assert parse_graph_node_file(tmp_path, "product/checkout").metadata == (
        GraphNodeMetadata(title="Checkout", state="active")
    )


def test_create_node_rejects_existing_node(
    tmp_path: Path,
) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Original\n")

    with pytest.raises(ValueError, match="already exists"):
        store.create_node("product/checkout", "Checkout", "Replacement\n")

    assert store.read_node("product/checkout").body == "Original\n"


def test_read_node_children_by_depth(
    tmp_path: Path,
) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Checkout body\n")
    store.create_node("product/search", "Search", "Search body\n")
    store.create_node("product/checkout/payment", "Payment", "Payment body\n")

    shallow = store.read_node("product", depth=1)
    deep = store.read_node("product", depth=2)

    assert shallow.semantic_path == "product"
    assert shallow.metadata.title == "Product"
    assert shallow.body == ""
    assert [
        (child.semantic_path, child.title, child.state) for child in shallow.children
    ] == [
        ("product/checkout", "Checkout", "active"),
        ("product/search", "Search", "active"),
    ]
    assert [child.semantic_path for child in deep.children] == [
        "product/checkout",
        "product/checkout/payment",
        "product/search",
    ]


def test_read_node_archived_children(
    tmp_path: Path,
) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/old", "Old", "Old body\n")
    store.create_node("product/old/deep", "Deep", "Deep body\n")
    store.update_node_metadata(
        "product/old", state="archived", archive_reason="Replaced by new tree"
    )

    node = store.read_node("product", depth=2)

    assert [
        (child.semantic_path, child.title, child.state) for child in node.children
    ] == [("product/old", "Old", "archived")]


def test_update_node_metadata_preserves_body(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Body\n")

    archived = store.update_node_metadata(
        "product/checkout",
        title="Old checkout",
        state="archived",
        archive_reason="No longer current",
    )
    active = store.update_node_metadata(
        "product/checkout", title="Checkout", state="active", archive_reason=""
    )

    assert archived.metadata == GraphNodeMetadata(
        title="Old checkout", state="archived", archive_reason="No longer current"
    )
    assert active.metadata == GraphNodeMetadata(title="Checkout", state="active")
    assert active.body == "Body\n"


@pytest.mark.parametrize(
    ("kwargs", "expected_message"),
    [
        ({"state": "archived"}, "archive_reason"),
        ({"title": ""}, "title"),
        ({"state": "active", "archive_reason": "still archived"}, "active"),
        ({"state": "draft"}, "state"),
    ],
)
def test_update_node_metadata_rejects_invalid(
    tmp_path: Path, kwargs: dict[str, Any], expected_message: str
) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Body\n")

    with pytest.raises(ValueError, match=expected_message):
        store.update_node_metadata("product/checkout", **kwargs)

    assert store.read_node("product/checkout").metadata == GraphNodeMetadata(
        title="Checkout", state="active"
    )


def test_write_node_ignores_preexisting_temp_symlink(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    node_dir = tmp_path / ".jri" / "graph" / "product"
    node_dir.mkdir(parents=True)
    (node_dir / ".NODE.md.tmp").symlink_to(outside)

    store.create_node("product", "Product", "Body\n")

    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert store.read_node("product").body == "Body\n"
    assert (node_dir / ".NODE.md.tmp").is_symlink()


def test_move_node_moves_subtree(
    tmp_path: Path,
) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Checkout body\n")
    store.create_node("product/checkout/payment", "Payment", "Payment body\n")

    moved = store.move_node("product/checkout", "platform/shop/checkout")

    assert moved.semantic_path == "platform/shop/checkout"
    assert store.read_node("platform").metadata.title == "Platform"
    assert store.read_node("platform/shop").metadata.title == "Shop"
    assert store.read_node("platform/shop/checkout").body == "Checkout body\n"
    assert store.read_node("platform/shop/checkout/payment").body == "Payment body\n"
    with pytest.raises(FileNotFoundError, match="not found"):
        store.read_node("product/checkout")


def test_move_node_failure_removes_auto_created_destination_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Checkout body\n")
    original_replace = Path.replace

    def fail_move(path: Path, target: Path) -> Path:
        if path == tmp_path / ".jri" / "graph" / "product" / "checkout":
            raise OSError("forced move failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_move)

    with pytest.raises(OSError, match="forced move failure"):
        store.move_node("product/checkout", "platform/shop/checkout")

    assert store.read_node("product/checkout").body == "Checkout body\n"
    with pytest.raises(FileNotFoundError, match="not found"):
        store.read_node("platform")
    assert not (tmp_path / ".jri" / "graph" / "platform").exists()


@pytest.mark.parametrize(
    ("source", "destination", "expected_message"),
    [
        ("product", "product/checkout", "own subtree"),
        ("product", "existing", "already exists"),
        ("product", "bad//path", "empty segments"),
        ("missing", "elsewhere", "not found"),
        ("product", "product", "already exists"),
    ],
)
def test_move_node_rejects_invalid_moves(
    tmp_path: Path, source: str, destination: str, expected_message: str
) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Body\n")
    store.create_node("existing", "Existing", "Existing\n")
    before = store.read_node("product", depth=2)

    with pytest.raises((ValueError, FileNotFoundError), match=expected_message):
        store.move_node(source, destination)

    after = store.read_node("product", depth=2)
    assert after == before
    assert store.read_node("product/checkout").body == "Body\n"


def test_move_node_rejects_root_moves(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)

    with pytest.raises(ValueError, match="root"):
        store.move_node("", "product")


def test_graph_store_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    graph_dir = tmp_path / ".jri" / "graph"
    graph_dir.mkdir(parents=True)
    (graph_dir / "linked").symlink_to(outside, target_is_directory=True)
    store = GraphStore(tmp_path)

    with pytest.raises(ValueError, match="outside `.jri/graph/`"):
        store.create_node("linked/child", "Child", "Body\n")
