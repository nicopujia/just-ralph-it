import sys
from pathlib import Path

import pytest

from jri.lib import files
from tests.conftest import CreateLink

# A lone surrogate is the shortest string that UTF-8 cannot encode.
UNENCODABLE_CONTENT = "\ud800"
READABLE_BY_EVERYONE = 0o644


def test_replaces_a_file_in_one_step(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "file.txt"

    files.write_atomically(target, "first")
    files.write_atomically(target, "second")

    assert target.read_text(encoding="utf-8") == "second"
    assert list(target.parent.iterdir()) == [target]


def test_writes_contents_outside_ascii_as_utf_8(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"

    files.write_atomically(target, "café ☕ 日本語")

    assert target.read_bytes() == "café ☕ 日本語".encode()


def test_empties_a_file_whose_new_contents_are_empty(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    files.write_atomically(target, "first")

    files.write_atomically(target, "")

    assert target.read_bytes() == b""


@pytest.mark.skipif(
    sys.platform == "win32", reason="a mode is POSIX; `chmod` on Windows sets the read-only flag and nothing else"
)
def test_keeps_the_permissions_of_the_file_it_replaces(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("first", encoding="utf-8")
    target.chmod(READABLE_BY_EVERYONE)

    files.write_atomically(target, "second")

    assert target.stat().st_mode & 0o777 == READABLE_BY_EVERYONE


def test_writes_through_a_symlink_instead_of_replacing_it(tmp_path: Path, create_link: CreateLink) -> None:
    target = tmp_path / "file.txt"
    target.write_text("first", encoding="utf-8")
    link = tmp_path / "link.txt"
    create_link(link, target)

    files.write_atomically(link, "second")

    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "second"


def test_leaves_no_temporary_file_behind_when_the_write_fails(tmp_path: Path) -> None:
    occupied = tmp_path / "occupied"
    occupied.mkdir()

    # Every platform refuses a file at a directory path.
    # Each platform returns its own error for this condition.
    with pytest.raises(OSError, match="occupied"):
        files.write_atomically(occupied, "content")

    assert list(tmp_path.iterdir()) == [occupied]


def test_leaves_no_temporary_file_behind_when_the_contents_cannot_be_encoded(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("first", encoding="utf-8")

    with pytest.raises(UnicodeEncodeError):
        files.write_atomically(target, UNENCODABLE_CONTENT)

    assert list(tmp_path.iterdir()) == [target]


def test_keeps_the_previous_contents_when_the_write_fails(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    files.write_atomically(target, "first")

    with pytest.raises(UnicodeEncodeError):
        files.write_atomically(target, UNENCODABLE_CONTENT)

    assert target.read_text(encoding="utf-8") == "first"


def test_removes_a_directory_and_all_data_below_it(tmp_path: Path) -> None:
    directory = tmp_path / "worktree"
    (directory / "nested").mkdir(parents=True)
    (directory / "nested" / "file.txt").write_text("what a killed run left", encoding="utf-8")

    files.remove_directory(directory)
    files.remove_directory(directory)

    assert not directory.exists()


@pytest.mark.skipif(
    sys.platform == "win32", reason="a directory that refuses a write is an access list `chmod` cannot write"
)
def test_leaves_the_directory_it_is_refused_the_removal_of(tmp_path: Path) -> None:
    guarded = tmp_path / "guarded"
    directory = guarded / "worktree"
    directory.mkdir(parents=True)
    guarded.chmod(0o500)

    try:
        files.remove_directory(directory)
    finally:
        guarded.chmod(0o700)

    assert directory.exists()


def test_names_the_files_of_a_read_from_where_the_reader_stands() -> None:
    root = Path.cwd()

    described = files.describe_paths([str(root / "README.md"), str(root / "src" / "app.py")])

    assert described == "README.md and src/app.py"


def test_counts_the_files_of_a_read_too_long_to_name() -> None:
    root = Path.cwd()

    described = files.describe_paths([str(root / f"note{index}.md") for index in range(6)])

    assert described == "note0.md, note1.md, note2.md and 3 more"


def test_names_a_file_outside_the_working_directory_from_home() -> None:
    assert files.describe_paths([str(Path.home() / "notes" / "idea.md")]) == "~/notes/idea.md"


def test_names_a_file_under_neither_the_working_directory_nor_home_in_full() -> None:
    assert files.describe_paths(["/etc/hosts"]) == "/etc/hosts"
