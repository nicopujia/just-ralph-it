import webbrowser

__all__ = ["open_page"]


# Whether a browser was started on the page, and never whether the
# browser drew it: what a controller answers for is the process it
# spawned. The browser is the preferred one and no other, where
# `webbrowser.open` walks the whole order until something answers yes:
# a fall-through picks a browser nothing looked at, and looking is the
# whole of what this adds.
def open_page(uri: str) -> bool:
    try:
        browser = webbrowser.get()
    # No browser this platform knows how to start, which is the answer
    # over SSH and on a machine with none installed.
    except webbrowser.Error:
        return False
    return False if _takes_the_terminal(browser) else browser.open(uri)


# A browser JRI must not start, because starting it is handing over the
# terminal the command was run in: it inherits the standard streams and
# is waited for, so the caller's next line lands after the user has
# quit it. A console browser is also the one that can make least of the
# page, which fetches mermaid and draws the graph in JavaScript.
#
# `GenericBrowser` is the class that waits, and `BackgroundBrowser` the
# subclass that does not. `UnixBrowser` says it of itself: `background`
# false is how the console browsers it covers are spelled, and it is
# what decides whether the spawn is given this process's streams.
def _takes_the_terminal(browser: webbrowser.BaseBrowser) -> bool:
    if isinstance(browser, webbrowser.BackgroundBrowser):
        return False
    if isinstance(browser, webbrowser.GenericBrowser):
        return True
    return isinstance(browser, webbrowser.UnixBrowser) and not browser.background
