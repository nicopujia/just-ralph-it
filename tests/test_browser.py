import sys
import time
import webbrowser
from pathlib import Path

import pytest

from jri.lib.browser import open_page

# This browser represents the browser that the machine would start.
# It records that it ran and the page that it received.
# `-c` excludes the script from `sys.argv`.
# So the marker is the first argument and the page is the second.
BROWSER = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')"
POLL = 0.01
PAGE = "file:///tmp/notes.html"
# JRI starts the browser and does not wait for it.
# The assertion must wait instead, and a slow interpreter can use all of this time to start.
RUNS_WITHIN = 30.0


def build_command(marker: Path) -> list[str]:
    return [sys.executable, "-c", BROWSER, str(marker), "%s"]


# Wait for the page, not for the marker. The browser makes the file before
# it writes it, so an existing marker can still be empty.
def read_page(marker: Path) -> str:
    deadline = time.monotonic() + RUNS_WITHIN
    while True:
        page = marker.read_text(encoding="utf-8") if marker.exists() else ""
        if page:
            return page
        assert time.monotonic() < deadline, "the browser was never started"
        time.sleep(POLL)


def use_browser(monkeypatch: pytest.MonkeyPatch, browser: webbrowser.BaseBrowser) -> None:
    monkeypatch.setattr(webbrowser, "get", lambda *_: browser)


def test_starts_a_browser_that_leaves_this_terminal_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "opened"
    use_browser(monkeypatch, webbrowser.BackgroundBrowser(build_command(marker)))

    assert open_page(PAGE)

    assert read_page(marker) == PAGE


# This is the class that `webbrowser` uses for lynx, w3m, and links.
# Starting it gives the command the current terminal.
# It inherits standard streams and the caller waits for it.
# The test can verify only that the browser did not start.
# A missing marker means that no process started.
# Do not infer where browser output would go.
def test_leaves_a_browser_that_would_take_this_terminal_unstarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "opened"
    use_browser(monkeypatch, webbrowser.GenericBrowser(build_command(marker)))

    assert not open_page(PAGE)

    assert not marker.exists()


# `Elinks` is one browser in this class.
# `webbrowser` selects it before other console browsers.
# It runs in the terminal when it does not display the page already.
# The `background` value controls this behavior.
# This command replaces the browser command for the test.
# A JRI that did not read that value would start the browser, and the browser would make the marker.
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
