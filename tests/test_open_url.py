"""open_url: the fast path that replaced a 12-step vision loop.

Everything here runs against the real function with the shell calls stubbed
out, so the assertions are about the exact URL and browser that *would* be
launched. No browser is opened.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import os_tools  # noqa: E402
from tools.os_tools import _resolve_site, open_application, open_url  # noqa: E402


class _Launches:
    """Captures what open_url would have handed to the OS."""

    def __init__(self):
        self.startfile = []
        self.popen = []

    def install(self, stack):
        stack.enter_context(
            mock.patch.object(os_tools.os, "startfile", self.startfile.append, create=True)
        )
        stack.enter_context(
            mock.patch.object(os_tools.subprocess, "Popen", self._popen)
        )
        return self

    def _popen(self, args, **kwargs):
        self.popen.append(list(args))
        return mock.Mock()

    @property
    def url(self):
        """The single URL that was opened, however it was opened."""
        if self.startfile:
            return self.startfile[-1]
        # Browsers are launched directly: [<browser exe>, <url>].
        return self.popen[-1][-1] if self.popen else None

    @property
    def browser(self):
        """Which browser executable was launched, as a bare name."""
        if not self.popen:
            return None
        import os.path
        exe = os.path.basename(str(self.popen[-1][0])).lower()
        return exe[:-4] if exe.endswith(".exe") else exe


class _LaunchCase(unittest.TestCase):
    def launch(self, *args, **kwargs):
        import contextlib
        with contextlib.ExitStack() as stack:
            spy = _Launches().install(stack)
            result = open_url(*args, **kwargs)
        return spy, result


class TestSiteResolution(unittest.TestCase):
    """Spoken site names -> canonical addresses."""

    def test_common_names(self):
        cases = {
            "youtube": "https://www.youtube.com",
            "gmail": "https://mail.google.com",
            "github": "https://github.com",
            "google": "https://www.google.com",
        }
        for spoken, expected in cases.items():
            with self.subTest(spoken=spoken):
                self.assertEqual(_resolve_site(spoken), expected)

    def test_speech_recognition_manglings(self):
        """The transcript is what reaches the model, not what was said."""
        for spoken in ("you tube", "YouTube", "  youtube  ", "youtube.com", "www.youtube.com"):
            with self.subTest(spoken=spoken):
                self.assertEqual(_resolve_site(spoken), "https://www.youtube.com")

    def test_full_urls_are_never_rewritten(self):
        """A deep link must survive intact, not collapse to the site root."""
        deep = "https://www.youtube.com/watch?v=abc123&t=90"
        self.assertEqual(_resolve_site(deep), deep)
        self.assertEqual(
            _resolve_site("https://docs.python.org/3/library/unittest.html"),
            "https://docs.python.org/3/library/unittest.html",
        )

    def test_bare_domains_get_https(self):
        self.assertEqual(_resolve_site("example.com"), "https://example.com")

    def test_bare_domain_with_a_path_keeps_the_path(self):
        """The alias map must not swallow everything after the host.

        "youtube.com/watch?v=abc" resolving to the YouTube home page is the
        same failure the user reported — landing somewhere they didn't ask for,
        while the reply claims success.
        """
        cases = {
            "youtube.com/watch?v=abc&t=90": "https://youtube.com/watch?v=abc&t=90",
            "github.com/torvalds/linux": "https://github.com/torvalds/linux",
            "reddit.com/r/python": "https://reddit.com/r/python",
            "twitter.com/jack": "https://twitter.com/jack",
        }
        for spoken, expected in cases.items():
            with self.subTest(spoken=spoken):
                self.assertEqual(_resolve_site(spoken), expected)

    def test_bare_domain_with_trailing_slash_still_uses_the_alias(self):
        self.assertEqual(_resolve_site("youtube.com/"), "https://www.youtube.com")

    def test_empty_input(self):
        self.assertEqual(_resolve_site("   "), "")


class TestOpenUrl(_LaunchCase):
    """The user's actual failing command, and its neighbours."""

    def test_the_original_bug_report(self):
        """'open youtube on my chrome browser' -> YouTube home, in Chrome, once."""
        spy, result = self.launch("youtube", browser="chrome")
        self.assertEqual(spy.url, "https://www.youtube.com")
        self.assertEqual(spy.browser, "chrome")
        # Exactly one launch: no browser-then-navigate two-step.
        self.assertEqual(len(spy.popen) + len(spy.startfile), 1)
        self.assertIn("youtube.com", result)

    def test_no_browser_uses_default(self):
        spy, _ = self.launch("youtube")
        self.assertEqual(spy.startfile, ["https://www.youtube.com"])
        self.assertEqual(spy.popen, [])

    def test_unknown_browser_falls_back_to_default(self):
        """Never shell out to an unrecognised executable name."""
        spy, _ = self.launch("youtube", browser="netscape navigator")
        self.assertEqual(spy.startfile, ["https://www.youtube.com"])
        self.assertEqual(spy.popen, [])

    def test_mangled_browser_name(self):
        spy, _ = self.launch("youtube", browser="crome")
        self.assertEqual(spy.browser, "chrome")


class TestSearch(_LaunchCase):
    """Site searches go straight to a results URL, never a click loop."""

    def test_youtube_search_uses_results_url(self):
        spy, _ = self.launch("youtube", search_query="lofi beats")
        self.assertEqual(
            spy.url, "https://www.youtube.com/results?search_query=lofi+beats"
        )

    def test_search_terms_are_escaped(self):
        spy, _ = self.launch("google", search_query="c++ & rust?")
        self.assertIn("q=c%2B%2B+%26+rust%3F", spy.url)
        # The raw characters must not survive into the URL unencoded.
        self.assertNotIn(" ", spy.url)

    def test_site_without_search_endpoint_scopes_a_web_search(self):
        spy, _ = self.launch("example.com", search_query="pricing")
        self.assertTrue(spy.url.startswith("https://www.google.com/search?q="))
        self.assertIn("site%3Aexample.com", spy.url)

    def test_blank_search_query_is_ignored(self):
        spy, _ = self.launch("youtube", search_query="   ")
        self.assertEqual(spy.url, "https://www.youtube.com")


class TestUrlSafety(_LaunchCase):
    """The model's output is not a trusted source of URIs."""

    def test_non_web_schemes_are_refused(self):
        for hostile in ("file:///C:/Windows/System32", "javascript:alert(1)",
                        "ms-settings:", "vbscript:x"):
            with self.subTest(url=hostile):
                spy, result = self.launch(hostile)
                self.assertEqual(spy.startfile, [], f"{hostile} reached the shell")
                self.assertEqual(spy.popen, [], f"{hostile} reached the shell")
                self.assertIn("not a website", result)

    def test_empty_url_is_refused(self):
        spy, result = self.launch("")
        self.assertEqual(spy.startfile, [])
        self.assertEqual(result, "No website given.")


class TestNoShellInjection(_LaunchCase):
    """URLs must never reach a command interpreter.

    `cmd /c start "" chrome <url>` splits on & | ^ < >, and Python only quotes
    arguments containing spaces. So https://site.com/watch?v=a&t=90 lost
    everything from "&" onward *and* ran the remainder as a shell command —
    with the URL coming from the model, which is influenced by whatever is on
    screen. Browsers are now launched via CreateProcess directly.
    """

    _HOSTILE = "https://example.com/?a=1&calc.exe"

    def test_url_is_not_passed_through_cmd(self):
        spy, _ = self.launch(self._HOSTILE, browser="chrome")
        for argv in spy.popen:
            self.assertNotIn(
                "cmd", [str(a).lower() for a in argv[:1]],
                f"URL routed through cmd: {argv}",
            )

    def test_ampersand_url_survives_intact(self):
        """The correctness half: a normal deep link must not be truncated."""
        url = "https://www.youtube.com/watch?v=abc123&t=90s"
        spy, _ = self.launch(url, browser="chrome")
        launched = spy.url
        if launched is None:
            self.skipTest("no browser resolvable on this machine")
        self.assertEqual(
            launched, url,
            "The URL was altered on its way to the browser; a & would be "
            "truncated by cmd and the tail executed.",
        )

    def test_browser_launch_uses_a_real_executable(self):
        spy, _ = self.launch("youtube", browser="chrome")
        if not spy.popen:
            self.skipTest("Chrome not installed on this machine")
        exe = str(spy.popen[-1][0]).lower()
        self.assertTrue(
            exe.endswith(".exe"),
            f"expected a resolved executable path, got {exe!r}",
        )
        self.assertNotIn("cmd", exe)

    def test_metacharacters_in_search_query_are_encoded(self):
        spy, _ = self.launch("youtube", search_query="a & b | c > d")
        for ch in "&|>":
            self.assertNotIn(ch, spy.url.split("search_query=")[-1])


class TestOpenApplicationSafety(unittest.TestCase):
    """open_application used to os.startfile anything containing '://'."""

    def _run(self, name):
        import contextlib
        with contextlib.ExitStack() as stack:
            spy = _Launches().install(stack)
            result = open_application(name)
        return spy, result

    def test_unknown_uri_scheme_is_refused(self):
        spy, result = self._run("file:///C:/Windows/System32/cmd.exe")
        self.assertEqual(spy.startfile, [])
        self.assertIn("don't know how to open", result)

    def test_known_uri_scheme_still_works(self):
        spy, _ = self._run("whatsapp")
        self.assertEqual(spy.startfile, ["whatsapp://"])

    def test_a_url_sent_here_is_handled_as_a_url(self):
        """Rather than launching a browser at a blank page."""
        spy, _ = self._run("https://example.com")
        self.assertEqual(spy.startfile, ["https://example.com"])

    def test_normal_apps_are_unaffected(self):
        spy, _ = self._run("notepad")
        self.assertEqual(spy.popen[-1], ["cmd", "/c", "start", "", "notepad"])

    def test_a_sentence_is_never_launched(self):
        """The reported bug, at the tool level.

        Popen succeeding is not the launch succeeding: `start` fails inside the
        child, asynchronously, and with no console attached Windows reports it
        as a modal dialog the tool never sees. So it used to spawn cmd, get a
        "Windows cannot find 'youtube on my chrome browser'" box, and report
        "Launched YouTube on my Chrome browser." to the HUD regardless.
        """
        spy, result = self._run("youtube on my chrome browser")
        self.assertEqual(spy.popen, [], "a sentence reached cmd")
        self.assertEqual(spy.startfile, [])
        self.assertNotIn("Launched", result)
        self.assertIn("don't know how to open", result)

    def test_an_absent_app_is_refused_not_claimed(self):
        """Never report success for something that cannot be launched."""
        spy, result = self._run("definitelynotarealapp123")
        self.assertEqual(spy.popen, [])
        self.assertNotIn("Launched", result)
        self.assertIn("don't know how to open", result)

    def test_a_site_name_here_opens_the_site(self):
        """A site reaching the app tool is the same class of failure: `start
        youtube` resolves nothing. Mirrors the existing URL fallback."""
        spy, _ = self._run("youtube")
        self.assertEqual(spy.popen, [])
        self.assertEqual(spy.startfile, ["https://www.youtube.com"])

    def test_an_installed_app_is_still_launched_via_start(self):
        """The resolution check is a gate, not a replacement for `start`."""
        for app in ("notepad", "calculator", "file explorer"):
            with self.subTest(app=app):
                spy, result = self._run(app)
                self.assertEqual(len(spy.popen), 1, f"{app} was not launched")
                self.assertEqual(spy.popen[-1][:4], ["cmd", "/c", "start", ""])
                self.assertIn("Launched", result)


    def test_shell_metacharacters_are_refused(self):
        """open_application does go through cmd, so its input must be plain.

        `target` falls through to the raw model string when it is not a known
        alias, and cmd would execute anything after an unquoted &.
        """
        for hostile in ("notepad & calc", "notepad|calc", "foo>bar", "a^b"):
            with self.subTest(app=hostile):
                spy, result = self._run(hostile)
                self.assertEqual(spy.popen, [], f"{hostile!r} reached cmd")
                self.assertEqual(spy.startfile, [])
                self.assertIn("don't know how to open", result)


class TestExecutableResolution(unittest.TestCase):
    """_resolve_executable backs both open_url's browser= and the launch gate."""

    @staticmethod
    def _an_installed_reg_expand_sz_app():
        """Find one App Paths entry stored as REG_EXPAND_SZ, read independently.

        Deliberately does not go through _resolve_executable — that is the code
        under test. Asking it "did you find it?" cannot distinguish "not
        installed" from "the lookup is broken", which is exactly the bug here,
        so the existence check has to be made separately.

        Entries already on PATH are skipped: shutil.which would satisfy them
        before the registry branch ran, and the registry branch is the point.
        """
        import shutil
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                root = winreg.OpenKey(hive, os_tools._APP_PATHS_KEY)
            except OSError:
                continue
            with root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        name = winreg.EnumKey(root, i)
                        with winreg.OpenKey(root, name) as key:
                            raw, kind = winreg.QueryValueEx(key, "")
                    except OSError:
                        continue
                    if kind != winreg.REG_EXPAND_SZ or shutil.which(name):
                        continue
                    path = os.path.expandvars(str(raw)).strip().strip('"')
                    if "%" not in path and os.path.exists(path):
                        return name, path
        return None, None

    def test_reg_expand_sz_app_paths_entries_resolve(self):
        """winreg.QueryValue reads only REG_SZ and raises "The data is invalid"
        on REG_EXPAND_SZ — which is how Windows registers many of its own apps
        ("%ProgramFiles(x86)%\\Windows Media Player\\wmplayer.exe"). That made
        installed programs look absent: open_url(browser=...) would quietly use
        the default browser and report the named one "not found", and the
        launch gate in open_application would refuse an app that is right there.
        """
        name, expected = self._an_installed_reg_expand_sz_app()
        if name is None:
            self.skipTest("no REG_EXPAND_SZ App Paths entry on this machine")

        found = os_tools._resolve_executable(name)
        self.assertIsNotNone(
            found, f"{name} is installed at {expected} but did not resolve")
        self.assertTrue(os.path.exists(found), found)
        # Fully expanded — no %ProgramFiles(x86)% left behind.
        self.assertNotIn("%", found)


if __name__ == "__main__":
    unittest.main(verbosity=2)
