"""Use the owner's own Safari (macOS) for the penyedia commands, standard library only.

AppleScript's `do JavaScript` runs code in a Safari tab, so API calls go out from the owner's logged-in
penyedia.inaproc.id tab with Safari's own cookies and Cloudflare clearance. Nothing is copied out of Safari.
Needs Safari > Settings > Advanced > "Show features for web developers", then Develop > Developer Settings >
"Allow JavaScript from Apple Events", plus macOS's one-time permission for the terminal app to control Safari.
JavaScript is always handed to osascript as an argument, never spliced into the AppleScript source.
"""
import json
import subprocess
import time

SETTING_HELP = ("Safari blocks scripts from the terminal until you allow it: Safari > Settings > Advanced > tick "
                "\"Show features for web developers\", then in the menu bar Develop > Developer Settings… > tick "
                "\"Allow JavaScript from Apple Events\". Then run the command again.")
AUTOMATION_HELP = ("macOS has not allowed this terminal app to control Safari. Open System Settings > Privacy & "
                   "Security > Automation, allow your terminal app (e.g. Sawit or Terminal) to control Safari, "
                   "and run the command again.")

LIST_SCRIPT = [
    'set sep to ASCII character 9',
    'set out to ""',
    'tell application "Safari"',
    '  repeat with w in (every window)',
    '    set i to 0',
    '    repeat with t in (every tab of w)',
    '      set i to i + 1',
    '      set u to ""',
    '      try',
    '        set u to URL of t',
    '      end try',
    '      if u is missing value then set u to ""',
    '      set out to out & (id of w) & sep & i & sep & u & linefeed',
    '    end repeat',
    '  end repeat',
    'end tell',
    'return out',
]
JS_SCRIPT = ['on run argv',
             'tell application "Safari" to return do JavaScript (item 1 of argv) in tab ((item 2 of argv) as integer)'
             ' of window id ((item 3 of argv) as integer)',
             'end run']
NAV_SCRIPT = ['on run argv',
              'tell application "Safari" to set URL of tab ((item 2 of argv) as integer) of window id '
              '((item 3 of argv) as integer) to (item 1 of argv)',
              'end run']
OPEN_SCRIPT = ['on run argv',
               'tell application "Safari"',
               'activate',
               'if (count of windows) is 0 then',
               'make new document with properties {URL:(item 1 of argv)}',
               'else',
               'tell front window to set current tab to (make new tab with properties {URL:(item 1 of argv)})',
               'end if',
               'end tell',
               'end run']


class SafariError(Exception):
    pass


def _osascript(lines, args, runner, timeout=120):
    argv = ["osascript"]
    for line in lines:
        argv += ["-e", line]
    argv += [str(a) for a in args]
    try:
        p = runner(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise SafariError("osascript not found: the Safari mode only works on macOS (use --browser chrome)")
    except subprocess.TimeoutExpired:
        raise SafariError("Safari did not answer in time")
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        if "Allow JavaScript from Apple Events" in err:
            raise SafariError(SETTING_HELP)
        if "-1743" in err or "Not authorized to send Apple events" in err:
            raise SafariError(AUTOMATION_HELP)
        raise SafariError(f"Safari: {err or 'osascript failed'}")
    out = p.stdout
    return out[:-1] if out.endswith("\n") else out


class Safari:
    """Stands where inaproc_chrome.Browser stands for the penyedia commands."""

    reused = True

    def __init__(self, runner=subprocess.run):
        self.runner = runner

    @property
    def alive(self):
        return True

    def _list(self):
        rows = []
        for line in _osascript(LIST_SCRIPT, [], self.runner).splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[0].strip().isdigit():
                rows.append((int(parts[0]), int(parts[1]), parts[2]))
        return rows

    def tabs(self, url_prefix=""):
        return [SafariTab(self, w, i) for w, i, u in self._list() if u.startswith(url_prefix)]

    def find_tab(self, url_prefix):
        t = self.tabs(url_prefix)
        return t[0] if t else None

    def new_tab(self, url):
        _osascript(OPEN_SCRIPT, [url], self.runner)
        deadline = time.time() + 15
        while time.time() < deadline:
            rows = self._list()
            for w, i, u in rows:
                if u.startswith(url.split("?")[0]) or u == url:
                    return SafariTab(self, w, i)
            time.sleep(0.3)
        # Safari may already have redirected (e.g. to a sign-in page): take the front window's current tab.
        rows = self._list()
        if rows:
            return SafariTab(self, rows[0][0], rows[0][1])
        raise SafariError("could not find the Safari tab that was just opened")

    def close(self):
        pass  # the owner's Safari stays as it is


class SafariTab:
    def __init__(self, safari, window_id, index):
        self.safari = safari
        self.window_id = window_id
        self.index = index
        self.target_id = f"safari:{window_id}:{index}"

    def _row(self):
        for w, i, u in self.safari._list():
            if (w, i) == (self.window_id, self.index):
                return u
        return None

    @property
    def closed(self):
        return self._row() is None

    def url(self):
        return self._row() or ""

    def evaluate(self, js, await_promise=False, timeout=120):
        """Run a synchronous JavaScript expression in the tab and return its JSON value."""
        wrapped = "JSON.stringify((function(){return (" + js + ");})())"
        out = _osascript(JS_SCRIPT, [wrapped, self.index, self.window_id], self.safari.runner, timeout=timeout)
        if out in ("", "missing value"):
            return None
        try:
            return json.loads(out)
        except ValueError:
            raise SafariError(f"unexpected answer from Safari: {out[:200]}")

    def navigate(self, url):
        _osascript(NAV_SCRIPT, [url, self.index, self.window_id], self.safari.runner)

    def wait_load(self, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if not self.url().startswith("about:") and self.evaluate("document.readyState") == "complete":
                    return True
            except SafariError as e:
                if str(e) in (SETTING_HELP, AUTOMATION_HELP):
                    raise
            time.sleep(0.5)
        return False

    def page_fetch(self, url, method="POST", headers=None, body=None, timeout=120):
        """Synchronous same-origin XMLHttpRequest inside the tab -> {status, headers, text}."""
        js = ("(function(){var x=new XMLHttpRequest();x.open(%s,%s,false);x.withCredentials=true;"
              "var h=%s;for(var k in h){x.setRequestHeader(k,h[k]);}x.send(%s);var rh={};"
              "x.getAllResponseHeaders().trim().split(/[\\r\\n]+/).forEach(function(l){var i=l.indexOf(':');"
              "if(i>0){rh[l.slice(0,i).trim().toLowerCase()]=l.slice(i+1).trim();}});"
              "return {status:x.status,headers:rh,text:x.responseText};})()"
              % (json.dumps(method), json.dumps(url), json.dumps(headers or {}),
                 "null" if body is None else json.dumps(body)))
        return self.evaluate(js, timeout=timeout)

    def close(self):
        pass
