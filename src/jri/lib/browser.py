import webbrowser

__all__ = ["open_page"]


# Return true only if the preferred browser starts for this page.
# Do not report that the browser displays the page. `webbrowser.open`
# can start other browsers after a failure. This function uses only the
# preferred browser, because it only starts that browser.
def open_page(uri: str) -> bool:
    try:
        browser = webbrowser.get()
    # Return false if this platform has no browser that it can start. This
    # can occur through SSH or on a system with no installed browser.
    except webbrowser.Error:
        return False
    return False if _takes_the_terminal(browser) else browser.open(uri)


# Do not start a browser that uses the terminal. It gets the standard
# streams and waits until it exits. The output from the next caller then
# appears only after the user exits the browser. A console browser also
# cannot completely display a page that fetches its libraries and makes
# its content with JavaScript.
#
# `GenericBrowser` waits. `BackgroundBrowser` does not wait.
# `UnixBrowser.background` is false for its console browsers. This value
# controls whether the new process gets this process's standard streams.
def _takes_the_terminal(browser: webbrowser.BaseBrowser) -> bool:
    if isinstance(browser, webbrowser.BackgroundBrowser):
        return False
    if isinstance(browser, webbrowser.GenericBrowser):
        return True
    return isinstance(browser, webbrowser.UnixBrowser) and not browser.background
