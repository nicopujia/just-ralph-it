import json
from pathlib import Path

import pytest

import jri.core.agents.bundle._shared.tools.graph as graph_tools_module
from jri.core.agents.bundle._shared.tools.graph import (
    run_apply_graph_patch,
    run_compile_graph,
    run_create_node,
    run_list_nodes,
    run_move_node,
    run_read_node,
    run_search_nodes,
    run_update_node_metadata,
)
from jri.core.graph import GraphStore


def test_graph_tool_handlers_reject_unexpected_payloads() -> None:
    with pytest.raises(ValueError, match="compile-graph does not accept arguments"):
        run_compile_graph({"unexpected": True})

    with pytest.raises(ValueError, match="list-nodes does not accept arguments"):
        run_list_nodes({"unexpected": True})


def test_graph_tool_handlers_validate_optional_payloads_and_return_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    created = json.loads(run_create_node({"path": "product/checkout", "title": "Checkout"}))
    assert created == {"path": "product/checkout", "auto_created_parents": ["product"]}

    child = json.loads(run_create_node({"path": "product/checkout/payment", "title": "Payment"}))
    assert child == {"path": "product/checkout/payment", "auto_created_parents": []}

    with pytest.raises(ValueError, match="must be a string"):
        run_create_node({"path": "product/other", "title": "Other", "body": 12})

    updated = json.loads(run_update_node_metadata({"path": "product/checkout", "title": "Checkout v2"}))
    assert updated == {"path": "product/checkout", "metadata": {"title": "Checkout v2", "state": "active"}}

    with pytest.raises(ValueError, match="active or archived"):
        run_update_node_metadata({"path": "product/checkout", "title": "Checkout", "state": "draft"})

    read = json.loads(run_read_node({"path": "product/checkout"}))
    assert read["path"] == "product/checkout"
    assert [child["path"] for child in read["children"]] == ["product/checkout/payment"]

    with pytest.raises(ValueError, match="non-negative integer"):
        run_read_node({"path": "product/checkout", "depth": True})

    search = json.loads(run_search_nodes({"query": "checkout", "limit": 1}))
    assert [match["path"] for match in search["matches"]] == ["product/checkout"]

    with pytest.raises(ValueError, match="non-negative integer"):
        run_search_nodes({"query": "checkout", "limit": True})

    patch = json.loads(
        run_apply_graph_patch({
            "patch": """*** Begin Graph Patch
*** Update Node: product/checkout
@@
+Checkout body
*** End Graph Patch"""
        })
    )
    assert patch == {"changed_nodes": [{"path": "product/checkout", "additions": 1, "deletions": 0}]}

    moved = json.loads(run_move_node({"source_path": "product/checkout", "destination_path": "platform/shop/checkout"}))
    assert moved == {"old_path": "product/checkout", "new_path": "platform/shop/checkout", "moved_subtree_count": 2}


def test_graph_tool_handlers_cover_remaining_public_wrapper_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    GraphStore(tmp_path).create_node("product", "Product", "Body\n")

    class DummyService:
        def compile_graph(self) -> dict[str, object]:
            return {"compiled": True}

    def fake_service(root: Path) -> DummyService:
        del root
        return DummyService()

    monkeypatch.setattr(graph_tools_module, "service", fake_service)

    assert json.loads(run_compile_graph({})) == {"compiled": True}
    assert json.loads(run_list_nodes({})) == {"nodes": [{"path": "product", "title": "Product", "state": "active"}]}

    with pytest.raises(ValueError, match="non-empty string"):
        run_search_nodes({"query": ""})

    with pytest.raises(ValueError, match="at least one metadata field"):
        run_update_node_metadata({"path": "product"})

    with pytest.raises(ValueError, match="must be a boolean"):
        run_search_nodes({"query": "product", "include_archived": "yes"})

    archived = json.loads(
        run_update_node_metadata({
            "path": "product",
            "title": "Product",
            "state": "archived",
            "archive_reason": "Retired",
        })
    )
    assert archived == {
        "path": "product",
        "metadata": {"title": "Product", "state": "archived", "archive_reason": "Retired"},
    }


def test_run_move_node_reports_missing_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(FileNotFoundError, match="not found"):
        run_move_node({"source_path": "missing", "destination_path": "elsewhere"})
