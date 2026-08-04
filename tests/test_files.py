from pathlib import Path

import pytest

from jri.lib import files


def test_replaces_a_file_in_one_step(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "file.txt"

    files.write_atomically(target, "first")
    files.write_atomically(target, "second")

    assert target.read_text(encoding="utf-8") == "second"
    assert list(target.parent.iterdir()) == [target]


def test_leaves_no_temporary_file_behind_when_the_write_fails(tmp_path: Path) -> None:
    occupied = tmp_path / "occupied"
    occupied.mkdir()

    with pytest.raises(IsADirectoryError):
        files.write_atomically(occupied, "content")

    assert list(tmp_path.iterdir()) == [occupied]
