import types
from io import StringIO
from pathlib import Path

import pytest

import jri.core.agents.bundle._shared.tools as tools_package
from jri.core.agents.bundle._shared.tools._validation import (
    apply_exact_edits,
    assert_exact_edits,
    assert_slug,
    assert_slug_list,
    assert_string_list,
    diff_text,
    ensure_expected_real_path,
    ensure_task_path_within,
    load_payload,
    print_result,
    read_task,
    read_task_source,
    repo_root_child,
    serialize_task,
    service,
    slugify,
    task_dirs,
)


def test_slug_and_list_validation_helpers_normalize_and_reject_invalid_values() -> None:
    assert assert_slug("slug", "  task-1  ") == "task-1"
    assert assert_string_list("names", None) is None
    assert assert_slug_list("names", None) is None
    assert assert_slug_list("names", ["  first  ", "second"]) == ["first", "second"]
    assert slugify("  Add quality gate  ") == "add-quality-gate"

    with pytest.raises(ValueError, match="must be a non-empty string"):
        assert_slug("slug", "   ")
    with pytest.raises(ValueError, match="not allowed"):
        assert_slug("slug", "bad slug")
    with pytest.raises(ValueError, match="must be a list of non-empty strings"):
        assert_string_list("names", "nope")
    with pytest.raises(ValueError, match="must be a list of non-empty strings"):
        assert_string_list("names", ["ok", ""])
    with pytest.raises(ValueError, match="must not contain duplicates"):
        assert_string_list("names", ["dup", "dup"])
    with pytest.raises(ValueError, match="could not derive a valid slug"):
        slugify("   ")


def test_path_helpers_return_real_paths_and_reject_escape_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    child_dir = ensure_expected_real_path(tmp_path, "tasks")
    assert child_dir == tmp_path / "tasks"

    link_root = tmp_path / "linked"
    real_root = tmp_path / "real"
    real_root.mkdir()
    link_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(ValueError, match="refusing to write outside"):
        ensure_expected_real_path(link_root, "tasks")

    task_dir = ensure_task_path_within(tmp_path, "quality-gate")
    assert task_dir == (tmp_path / "quality-gate.md").resolve()

    with pytest.raises(ValueError, match="refusing to write outside"):
        ensure_task_path_within(tmp_path, "../escape")

    repo_child = repo_root_child("README.md")
    assert repo_child == tmp_path / "README.md"

    with pytest.raises(ValueError, match="refusing to access outside repo root"):
        repo_root_child("../escape")

    todo_dir, doing_dir, done_dir = task_dirs(tmp_path)
    assert todo_dir == tmp_path / ".jri" / "tasks" / "todo"
    assert doing_dir == tmp_path / ".jri" / "tasks" / "doing"
    assert done_dir == tmp_path / ".jri" / "tasks" / "done"


def test_task_source_and_exact_edit_validation_behaviors(tmp_path: Path) -> None:
    task_path = tmp_path / "task.md"
    task_path.write_text(
        "---\n"
        'title: "Add quality gate"\n'
        "priority: 1\n"
        'assignee: "Ralph"\n'
        "depends_on:\n"
        '  - "task-a"\n'
        "acceptance_criteria:\n"
        '  - "make check passes"\n'
        "---\n\n"
        "Implement the quality gate.\n",
        encoding="utf-8",
    )

    metadata, body = read_task(task_path)
    assert metadata["title"] == "Add quality gate"
    assert metadata["depends_on"] == ["task-a"]
    assert body == "Implement the quality gate.\n"

    metadata_again, body_again = read_task_source(task_path, task_path.read_text(encoding="utf-8"))
    assert metadata_again == metadata
    assert body_again == body

    minimal_task_path = tmp_path / "minimal.md"
    minimal_task_path.write_text(
        '---\ntitle: "Minimal task"\npriority: 0\nassignee: "Human"\n---\n\nBody only.\n', encoding="utf-8"
    )
    minimal_metadata, minimal_body = read_task_source(minimal_task_path, minimal_task_path.read_text(encoding="utf-8"))
    assert minimal_metadata["title"] == "Minimal task"
    assert minimal_metadata.get("depends_on") is None
    assert minimal_metadata.get("acceptance_criteria") is None
    assert minimal_body == "Body only.\n"

    with pytest.raises(ValueError, match="invalid task format"):
        read_task_source(task_path, "no frontmatter")
    with pytest.raises(ValueError, match="invalid task metadata object"):
        read_task_source(task_path, "---\nfoo\n---\nbody\n")
    with pytest.raises(ValueError, match="depends_on"):
        read_task_source(
            task_path,
            "---\n"
            'title: "Add quality gate"\n'
            "priority: 1\n"
            'assignee: "Ralph"\n'
            "depends_on:\n"
            "  - 1\n"
            "acceptance_criteria:\n"
            '  - "make check passes"\n'
            "---\n\n"
            "Implement the quality gate.\n",
        )

    with pytest.raises(ValueError, match="invalid task format"):
        read_task_source(task_path, "---\nfoo\n")
    with pytest.raises(ValueError, match="invalid task metadata YAML"):
        read_task_source(task_path, "---\nfoo: [\n---\nbody\n")

    assert (
        serialize_task({"title": "alpha", "priority": 1}, "Body text\n")
        == "---\ntitle: alpha\npriority: 1\n---\n\nBody text\n"
    )

    edits = assert_exact_edits({"edits": [{"oldText": "alpha", "newText": "beta"}]})
    assert edits == [{"oldText": "alpha", "newText": "beta"}]
    assert apply_exact_edits("alpha", edits) == ("beta", 1)

    with pytest.raises(ValueError, match="non-empty list"):
        assert_exact_edits({"edits": []})
    with pytest.raises(ValueError, match="must be an object"):
        assert_exact_edits({"edits": ["oops"]})
    with pytest.raises(ValueError, match="oldText"):
        assert_exact_edits({"edits": [{"oldText": "", "newText": "beta"}]})
    with pytest.raises(ValueError, match="newText"):
        assert_exact_edits({"edits": [{"oldText": "alpha", "newText": 1}]})
    with pytest.raises(ValueError, match="was not found"):
        apply_exact_edits("alpha", [{"oldText": "missing", "newText": "beta"}])
    with pytest.raises(ValueError, match="make it unique"):
        apply_exact_edits("alpha alpha", [{"oldText": "alpha", "newText": "beta"}])

    assert diff_text("task.md", "old\n", "new\n").startswith("--- a/task.md\n")


def test_load_payload_and_print_result_use_stdio(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", StringIO("{bad json"), raising=False)

    with pytest.raises(ValueError, match="invalid JSON payload"):
        load_payload()

    monkeypatch.setattr("sys.stdin", StringIO('{"foo": 1}'), raising=False)
    assert load_payload() == {"foo": 1}

    print_result("done")
    assert capsys.readouterr().out == "done"


def test_load_payload_rejects_non_object_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(read=lambda: "[]"), raising=False)

    with pytest.raises(ValueError, match="must be a JSON object"):
        load_payload()


def test_service_uses_package_export_then_fallback_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class PackageService:
        def __init__(self, root: Path) -> None:
            self.root = root

    class FallbackService:
        def __init__(self, root: Path) -> None:
            self.root = root

    monkeypatch.setattr(tools_package, "JriService", PackageService)
    package_service = service(tmp_path)
    assert isinstance(package_service, PackageService)
    assert package_service.root == tmp_path

    monkeypatch.setattr(tools_package, "JriService", None)
    monkeypatch.setattr("jri.core.service.JriService", FallbackService, raising=False)

    fallback_service = service(tmp_path)
    assert isinstance(fallback_service, FallbackService)
    assert fallback_service.root == tmp_path
