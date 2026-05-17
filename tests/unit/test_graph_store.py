from pathlib import Path
from typing import Any

import pytest

import jri.core.graph as graph_module
from jri.core.graph import GraphStore, parse_graph_node_file
from jri.core.models import GraphNode, GraphNodeMetadata


def test_create_node_creates_parents(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)

    node = store.create_node("product/checkout/payment", "Payment", "Collect money.\n")

    assert node.semantic_path == "product/checkout/payment"
    assert node.metadata == GraphNodeMetadata(title="Payment", state="active")
    assert node.body == "Collect money.\n"
    assert parse_graph_node_file(tmp_path, "product").metadata == GraphNodeMetadata(title="Product", state="active")
    assert parse_graph_node_file(tmp_path, "product").body == ""
    assert parse_graph_node_file(tmp_path, "product/checkout").metadata == (
        GraphNodeMetadata(title="Checkout", state="active")
    )


def test_create_node_rejects_existing_node(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Original\n")

    with pytest.raises(ValueError, match="already exists"):
        store.create_node("product/checkout", "Checkout", "Replacement\n")

    assert store.read_node("product/checkout").body == "Original\n"


def test_read_node_children_by_depth(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Checkout body\n")
    store.create_node("product/search", "Search", "Search body\n")
    store.create_node("product/checkout/payment", "Payment", "Payment body\n")

    shallow = store.read_node("product", depth=1)
    deep = store.read_node("product", depth=2)

    assert shallow.semantic_path == "product"
    assert shallow.metadata.title == "Product"
    assert shallow.body == ""
    assert [(child.semantic_path, child.title, child.state) for child in shallow.children] == [
        ("product/checkout", "Checkout", "active"),
        ("product/search", "Search", "active"),
    ]
    assert [child.semantic_path for child in deep.children] == [
        "product/checkout",
        "product/checkout/payment",
        "product/search",
    ]


def test_read_node_archived_children(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/old", "Old", "Old body\n")
    store.create_node("product/old/deep", "Deep", "Deep body\n")
    store.update_node_metadata("product/old", state="archived", archive_reason="Replaced by new tree")

    node = store.read_node("product", depth=2)

    assert [(child.semantic_path, child.title, child.state) for child in node.children] == [
        ("product/old", "Old", "archived")
    ]


def test_read_node_skips_missing_child_node_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product", "Product", "Body\n")
    product_dir = tmp_path / ".jri" / "graph" / "product"
    (product_dir / "ghost").mkdir()

    def fake_validate_graph_tree(root: Path) -> None:
        del root

    monkeypatch.setattr(graph_module, "validate_graph_tree", fake_validate_graph_tree)

    node = store.read_node("product", depth=1)

    assert node.children == ()


def test_read_node_missing_parent_has_no_children(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product", "Product", "Product overview.\n")

    assert store.read_node("product", depth=1).children == ()


def test_list_nodes_returns_top_level_nodes_sorted(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Checkout body\n")
    store.create_node("decisions/pricing", "Pricing", "Pricing body\n")
    store.update_node_metadata("product", state="archived", archive_reason="Replaced by decisions")

    assert [(node.semantic_path, node.title, node.state) for node in store.list_nodes()] == [
        ("decisions", "Decisions", "active"),
        ("product", "Product", "archived"),
    ]


def test_list_nodes_allows_missing_graph(tmp_path: Path) -> None:
    assert GraphStore(tmp_path).list_nodes() == ()


def test_list_nodes_skips_missing_child_node_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = GraphStore(tmp_path)
    graph_dir = tmp_path / ".jri" / "graph"
    graph_dir.mkdir(parents=True)
    (graph_dir / "ghost").mkdir()

    def fake_validate_graph_tree(root: Path) -> None:
        del root

    monkeypatch.setattr(graph_module, "validate_graph_tree", fake_validate_graph_tree)

    assert store.list_nodes() == ()


def test_search_nodes_ignores_raw_root_node_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = GraphStore(tmp_path)
    graph_dir = tmp_path / ".jri" / "graph"
    graph_dir.mkdir(parents=True)
    (graph_dir / "NODE.md").write_text("---\ntitle: Root\nstate: active\n---\n\nneedle\n", encoding="utf-8")

    def fake_validate_graph_tree(root: Path) -> None:
        del root

    monkeypatch.setattr(graph_module, "validate_graph_tree", fake_validate_graph_tree)

    assert store.search_nodes("needle") == ()


def test_search_nodes_scores_and_filters_plain_files(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Send a confirmation email after payment succeeds.\n")
    store.create_node("product/search", "Product Search", "Find catalog products.\n")
    store.create_node("decisions/email", "Email Decisions", "Use transactional mail.\n")
    store.update_node_metadata("decisions/email", state="archived", archive_reason="Covered elsewhere")

    matches = store.search_nodes("confirmation email")

    assert [(match.semantic_path, match.title) for match in matches] == [("product/checkout", "Checkout")]
    assert matches[0].score > 0
    assert "confirmation email" in matches[0].snippet
    assert store.search_nodes("email", limit=1)[0].semantic_path == "product/checkout"
    assert any(match.semantic_path == "decisions/email" for match in store.search_nodes("email", include_archived=True))


def test_search_nodes_handles_missing_graph_and_empty_query_tokens(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)

    assert store.search_nodes("needle") == ()

    with pytest.raises(ValueError, match="non-negative"):
        store.search_nodes("needle", limit=-1)

    with pytest.raises(ValueError, match="searchable text"):
        store.search_nodes("!!!")


def test_search_nodes_returns_empty_and_ellipsized_snippets(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/empty", "Needle", "")
    store.create_node("product/long", "Long", "prefix " + ("alpha " * 20) + "needle" + (" beta" * 20) + " suffix\n")

    matches = store.search_nodes("needle")

    assert matches[0].snippet == ""
    assert matches[1].snippet.startswith("...")
    assert matches[1].snippet.endswith("...")


def test_search_nodes_rejects_empty_query(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        GraphStore(tmp_path).search_nodes("   ")


def test_update_node_metadata_preserves_body(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Body\n")

    archived = store.update_node_metadata(
        "product/checkout", title="Old checkout", state="archived", archive_reason="No longer current"
    )
    active = store.update_node_metadata("product/checkout", title="Checkout", state="active", archive_reason="")

    assert archived.metadata == GraphNodeMetadata(
        title="Old checkout", state="archived", archive_reason="No longer current"
    )
    assert active.metadata == GraphNodeMetadata(title="Checkout", state="active")
    assert active.body == "Body\n"


def test_update_node_metadata_preserves_existing_archive_reason(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Body\n")
    store.update_node_metadata("product/checkout", state="archived", archive_reason="Replaced")

    updated = store.update_node_metadata("product/checkout", state="archived")

    assert updated.metadata == GraphNodeMetadata(title="Checkout", state="archived", archive_reason="Replaced")


@pytest.mark.parametrize(
    ("kwargs", "expected_message"),
    [
        ({"archive_reason": 12}, "archive_reason"),
        ({"state": "archived"}, "archive_reason"),
        ({"title": ""}, "title"),
        ({"state": "active", "archive_reason": "still archived"}, "active"),
        ({"state": "draft"}, "state"),
    ],
)
def test_update_node_metadata_rejects_invalid(tmp_path: Path, kwargs: dict[str, Any], expected_message: str) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Body\n")

    with pytest.raises(ValueError, match=expected_message):
        store.update_node_metadata("product/checkout", **kwargs)

    assert store.read_node("product/checkout").metadata == GraphNodeMetadata(title="Checkout", state="active")


def test_read_node_rejects_negative_depth(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product", "Product", "Body\n")

    with pytest.raises(ValueError, match="non-negative"):
        store.read_node("product", depth=-1)


def test_write_node_cleans_up_temp_file_on_replace_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = GraphStore(tmp_path)
    node_path = tmp_path / ".jri" / "graph" / "product" / "NODE.md"
    node = GraphNode(
        path=node_path,
        semantic_path="product",
        metadata=GraphNodeMetadata(title="Product", state="active"),
        body="Body\n",
    )

    def fail_replace(_source: Path, _target: Path) -> Path:
        raise OSError("forced replace failure")

    monkeypatch.setattr(graph_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="forced replace failure"):
        store.write_node(node)

    assert not node.path.exists()
    temp_files = [entry for entry in node.path.parent.iterdir() if entry.name.startswith(".NODE.md.")]
    assert not temp_files


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


def test_move_node_moves_subtree(tmp_path: Path) -> None:
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


def test_move_node_failure_ignores_already_removed_auto_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Checkout body\n")
    original_replace = Path.replace

    def remove_destination_parent_then_fail(path: Path, target: Path) -> Path:
        if path == tmp_path / ".jri" / "graph" / "product" / "checkout":
            graph_module.shutil.rmtree(tmp_path / ".jri" / "graph" / "platform")
            raise OSError("forced move failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", remove_destination_parent_then_fail)

    with pytest.raises(OSError, match="forced move failure"):
        store.move_node("product/checkout", "platform/shop/checkout")

    assert store.read_node("product/checkout").body == "Checkout body\n"
    assert not (tmp_path / ".jri" / "graph" / "platform").exists()


def test_move_node_parent_creation_failure_removes_partial_destination_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GraphStore(tmp_path)
    store.create_node("product/checkout", "Checkout", "Checkout body\n")
    original_write_node = store.write_node

    def fail_on_second_parent(node: GraphNode) -> None:
        if node.semantic_path == "platform/shop":
            raise OSError("forced parent creation failure")
        original_write_node(node)

    monkeypatch.setattr(store, "write_node", fail_on_second_parent)

    with pytest.raises(OSError, match="forced parent creation failure"):
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
def test_move_node_rejects_invalid_moves(tmp_path: Path, source: str, destination: str, expected_message: str) -> None:
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
