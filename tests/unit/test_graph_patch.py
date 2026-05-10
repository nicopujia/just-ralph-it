from pathlib import Path

import pytest

from jri.core.graph import GraphStore, apply_graph_patch
from jri.core.models import GraphNode


def test_apply_graph_patch_updates_single_node_body(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("auth/oauth", "OAuth", "First line\nOld line\nLast line\n")

    summary = apply_graph_patch(
        store,
        """*** Begin Graph Patch
*** Update Node: auth/oauth
@@
 First line
-Old line
+New line
 Last line
*** End Graph Patch""",
    )

    assert store.read_node("auth/oauth").body == "First line\nNew line\nLast line\n"
    assert [(item.path, item.additions, item.deletions) for item in summary.nodes] == [
        ("auth/oauth", 1, 1)
    ]


def test_apply_graph_patch_updates_multiple_nodes_atomically(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("auth/oauth", "OAuth", "Use password flow.\n")
    store.create_node("auth/session", "Session", "Short TTL.\n")

    summary = apply_graph_patch(
        store,
        """*** Begin Graph Patch
*** Update Node: auth/oauth
@@
-Use password flow.
+Use OAuth code flow.
*** Update Node: auth/session
@@
-Short TTL.
+Rotating session TTL.
*** End Graph Patch""",
    )

    assert store.read_node("auth/oauth").body == "Use OAuth code flow.\n"
    assert store.read_node("auth/session").body == "Rotating session TTL.\n"
    assert [(item.path, item.additions, item.deletions) for item in summary.nodes] == [
        ("auth/oauth", 1, 1),
        ("auth/session", 1, 1),
    ]


def test_apply_graph_patch_counts_duplicate_shared_lines(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node(
        "auth/oauth",
        "OAuth",
        "Keep\nRepeated\nRepeated\nOld\nRepeated\nDone\n",
    )

    summary = apply_graph_patch(
        store,
        """*** Begin Graph Patch
*** Update Node: auth/oauth
@@
 Keep
 Repeated
-Repeated
-Old
+Repeated
+New
 Repeated
 Done
*** End Graph Patch""",
    )

    assert store.read_node("auth/oauth").body == (
        "Keep\nRepeated\nRepeated\nNew\nRepeated\nDone\n"
    )
    assert [(item.path, item.additions, item.deletions) for item in summary.nodes] == [
        ("auth/oauth", 2, 2)
    ]


@pytest.mark.parametrize(
    ("patch_text", "expected_message"),
    [
        ("", "empty patch"),
        ("*** Begin Patch\n*** End Patch", "Begin Graph Patch"),
        (
            "*** Begin Graph Patch\n"
            "*** Add Node: auth/oauth\n"
            "+Body\n"
            "*** End Graph Patch",
            "unsupported graph patch operation",
        ),
        (
            "*** Begin Graph Patch\n*** Delete Node: auth/oauth\n*** End Graph Patch",
            "unsupported graph patch operation",
        ),
        (
            "*** Begin Graph Patch\n"
            "*** Update Node: auth/oauth\n"
            "*** Move to: auth/openid\n"
            "*** End Graph Patch",
            "move",
        ),
    ],
)
def test_apply_graph_patch_rejects_invalid_envelope_or_operations(
    tmp_path: Path, patch_text: str, expected_message: str
) -> None:
    store = GraphStore(tmp_path)
    store.create_node("auth/oauth", "OAuth", "Body\n")

    with pytest.raises(ValueError, match=expected_message):
        apply_graph_patch(store, patch_text)

    assert store.read_node("auth/oauth").body == "Body\n"


def test_apply_graph_patch_rejects_missing_node(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)

    with pytest.raises(FileNotFoundError, match="not found"):
        apply_graph_patch(
            store,
            """*** Begin Graph Patch
*** Update Node: auth/missing
@@
+Body
*** End Graph Patch""",
        )


def test_apply_graph_patch_rejects_unmatched_context(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("auth/oauth", "OAuth", "Current body\n")

    with pytest.raises(ValueError, match="expected lines"):
        apply_graph_patch(
            store,
            """*** Begin Graph Patch
*** Update Node: auth/oauth
@@
-Stale body
+New body
*** End Graph Patch""",
        )

    assert store.read_node("auth/oauth").body == "Current body\n"


def test_apply_graph_patch_rejects_frontmatter_edits(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("auth/oauth", "OAuth", "Body\n")

    with pytest.raises(ValueError, match="frontmatter"):
        apply_graph_patch(
            store,
            """*** Begin Graph Patch
*** Update Node: auth/oauth
@@
+---
+title: Changed
+state: active
+---
 Body
*** End Graph Patch""",
        )

    assert store.read_node("auth/oauth").metadata.title == "OAuth"
    assert store.read_node("auth/oauth").body == "Body\n"


def test_apply_graph_patch_rejects_no_op_patch(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("auth/oauth", "OAuth", "Body\n")

    with pytest.raises(ValueError, match="no-op"):
        apply_graph_patch(
            store,
            """*** Begin Graph Patch
*** Update Node: auth/oauth
@@
 Body
*** End Graph Patch""",
        )


@pytest.mark.parametrize("raw_path", ["auth//oauth", "auth/NODE.md", "/auth/oauth"])
def test_apply_graph_patch_rejects_malformed_paths(
    tmp_path: Path, raw_path: str
) -> None:
    store = GraphStore(tmp_path)

    with pytest.raises(ValueError):
        apply_graph_patch(
            store,
            f"""*** Begin Graph Patch
*** Update Node: {raw_path}
@@
+Body
*** End Graph Patch""",
        )


def test_apply_graph_patch_failure_leaves_all_nodes_unchanged(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("auth/oauth", "OAuth", "Old OAuth\n")
    store.create_node("auth/session", "Session", "Old session\n")

    with pytest.raises(ValueError, match="expected lines"):
        apply_graph_patch(
            store,
            """*** Begin Graph Patch
*** Update Node: auth/oauth
@@
-Old OAuth
+New OAuth
*** Update Node: auth/session
@@
-Missing session
+New session
*** End Graph Patch""",
        )

    assert store.read_node("auth/oauth").body == "Old OAuth\n"
    assert store.read_node("auth/session").body == "Old session\n"


def test_apply_graph_patch_write_failure_restores_earlier_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GraphStore(tmp_path)
    store.create_node("auth/oauth", "OAuth", "Old OAuth\n")
    store.create_node("auth/session", "Session", "Old session\n")
    original_write_node = store.write_node

    def fail_on_session_write(node: GraphNode) -> None:
        if node.semantic_path == "auth/session" and node.body == "New session\n":
            raise OSError("forced later write failure")
        original_write_node(node)

    monkeypatch.setattr(store, "write_node", fail_on_session_write)

    with pytest.raises(OSError, match="forced later write failure"):
        apply_graph_patch(
            store,
            """*** Begin Graph Patch
*** Update Node: auth/oauth
@@
-Old OAuth
+New OAuth
*** Update Node: auth/session
@@
-Old session
+New session
*** End Graph Patch""",
        )

    assert store.read_node("auth/oauth").body == "Old OAuth\n"
    assert store.read_node("auth/session").body == "Old session\n"


def test_apply_graph_patch_allows_empty_final_body(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.create_node("auth/oauth", "OAuth", "Only line\n")

    summary = apply_graph_patch(
        store,
        """*** Begin Graph Patch
*** Update Node: auth/oauth
@@
-Only line
*** End Graph Patch""",
    )

    assert store.read_node("auth/oauth").body == ""
    assert [(item.path, item.additions, item.deletions) for item in summary.nodes] == [
        ("auth/oauth", 0, 1)
    ]
