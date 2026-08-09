import sys
import time
import webbrowser
from pathlib import Path

import pytest

from jri.lib.browser import open_page

# A browser stands for whatever the machine would have started, and the
# only thing this one does is write down that it ran and what it was
# handed. `-c` leaves the script out of `sys.argv`, so the marker is
# the first argument and the page the second.
BROWSER = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')"
POLL = 0.01
PAGE = "file:///tmp/notes.html"
# A spawn nothing waits for is one the assertion has to wait for, and
# the wait covers an interpreter starting on a machine under load.
RUNS_WITHIN = 30.0


def build_command(marker: Path) -> list[str]:
    return [sys.executable, "-c", BROWSER, str(marker), "%s"]


def use_browser(monkeypatch: pytest.MonkeyPatch, browser: webbrowser.BaseBrowser) -> None:
    monkeypatch.setattr(webbrowser, "get", lambda *_: browser)


def test_starts_a_browser_that_leaves_this_terminal_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "opened"
    use_browser(monkeypatch, webbrowser.BackgroundBrowser(build_command(marker)))

    assert open_page(PAGE)

    deadline = time.monotonic() + RUNS_WITHIN
    while not marker.exists():
        assert time.monotonic() < deadline, "the browser was never started"
        time.sleep(POLL)
    assert marker.read_text(encoding="utf-8") == PAGE


# The class below is the one `webbrowser` registers lynx, w3m and links
# as, and starting it hands over the terminal the command was run in:
# it inherits the standard streams and is waited for. So the answer a
# test can read is that the browser never ran at all -- a marker that
# is not there is a process that was never started -- rather than a
# claim about where its output went.
def test_leaves_a_browser_that_would_take_this_terminal_unstarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "opened"
    use_browser(monkeypatch, webbrowser.GenericBrowser(build_command(marker)))

    assert not open_page(PAGE)

    assert not marker.exists()


# `Elinks` is one of these, and `webbrowser` puts it ahead of every
# other console browser it finds. What such a browser does with a page
# no copy of it is already showing is run itself over the terminal and
# wait, and `background` is the declaration that decides it. The
# command below stands where the browser's own would, so a guard that
# stopped reading the declaration would leave the marker behind.
def test_leaves_a_unix_browser_that_stays_in_the_foreground_unstarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "opened"
    browser = webbrowser.UnixBrowser(sys.executable)
    browser.remote_args = build_command(marker)[1:]
    browser.remote_action = ""
    assert not browser.background, "a Unix browser is in the foreground unless it says otherwise"
    use_browser(monkeypatch, browser)

    assert not open_page(PAGE)

    assert not marker.exists()


def test_says_no_browser_opened_where_the_machine_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object) -> webbrowser.BaseBrowser:
        raise webbrowser.Error("could not locate runnable browser")

    monkeypatch.setattr(webbrowser, "get", refuse)

    assert not open_page(PAGE)


def test_says_no_browser_opened_where_the_one_it_found_will_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    use_browser(monkeypatch, webbrowser.BackgroundBrowser("jri-has-no-such-browser"))

    assert not open_page(PAGE)
