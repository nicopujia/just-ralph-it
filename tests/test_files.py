from pathlib import Path

import pytest

from jri.lib import files

# A lone surrogate is the shortest string utf-8 cannot encode.
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


def test_keeps_the_permissions_of_the_file_it_replaces(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("first", encoding="utf-8")
    target.chmod(READABLE_BY_EVERYONE)

    files.write_atomically(target, "second")

    assert target.stat().st_mode & 0o777 == READABLE_BY_EVERYONE


def test_writes_through_a_symlink_instead_of_replacing_it(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("first", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    files.write_atomically(link, "second")

    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "second"


def test_leaves_no_temporary_file_behind_when_the_write_fails(tmp_path: Path) -> None:
    occupied = tmp_path / "occupied"
    occupied.mkdir()

    with pytest.raises(IsADirectoryError):
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
