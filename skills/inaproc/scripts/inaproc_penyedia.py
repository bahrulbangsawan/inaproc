"""Seller (penyedia) side of the inaproc skill: the owner's own login, an API recorder, and product commands.

Every call to penyedia.inaproc.id runs as fetch() inside a penyedia page of the skill's own Chrome profile
(`~/.inaproc/chrome-penyedia`), so cookies, token refresh and Cloudflare clearance stay in the browser.
The skill never sees, stores or types the password; the owner signs in in the Chrome window.
"""
import datetime as _dt
import json
import os
import re
import time
import urllib.parse

import inaproc_chrome as ic

PENYEDIA = "https://penyedia.inaproc.id"
GRAPHQL = PENYEDIA + "/graphql"
R = "<redacted>"
SECRET_NAME = re.compile(r"token|secret|passw|session|csrf|signature|credential|authorization|cookie", re.I)
SECRET_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization"}
TRACKER_HOSTS = ("google-analytics.com", "googletagmanager.com", "clarity.ms", "facebook.net", "doubleclick.net")
LOGOUT_ORIGINS = [PENYEDIA, "https://daftar-akun.inaproc.id", "https://login-akun.inaproc.id",
                  "https://akun.inaproc.id", "https://katalog.inaproc.id"]

# Runs inside a penyedia page. Returns only what the CLI prints: never the raw session (it may hold tokens).
SESSION_JS = r"""(async()=>{
 try{
  const r=await fetch('/api/auth/session',{credentials:'include',headers:{accept:'application/json'}});
  let j=null; try{ j=await r.json(); }catch(e){}
  const u=(j&&j.user)||{};
  const pick=(...xs)=>{for(const x of xs){if(x!==undefined&&x!==null&&x!=='')return String(x);}return null;};
  return {signedIn: !!(j && (j.user || j.accessToken || j.expires)), endpointStatus: r.status,
          name: pick(u.name,u.fullName,u.fullname,u.username,u.email && u.email.split('@')[0]),
          company: pick(u.companyName,u.company && u.company.name,u.company,u.sellerName,u.institutionName),
          expires: pick(j && j.expires)};
 }catch(e){ return {signedIn:false, error:String(e)}; }
})()"""


class NotSignedIn(Exception):
    exit_code = 3


def _err():
    from inaproc import InaprocError
    return InaprocError


# ----------------------------------------------------------------------------- redaction + private files
def _secret_name(name):
    n = str(name).lower()
    return bool(SECRET_NAME.search(n)) or n.endswith("key")


def redact(obj):
    if isinstance(obj, dict):
        return {k: (R if _secret_name(k) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str) and obj.startswith(("http://", "https://")):
        return redact_url(obj)
    return obj


def redact_headers(headers):
    return {k: (R if k.lower() in SECRET_HEADERS or _secret_name(k) else v) for k, v in (headers or {}).items()}


def redact_url(url):
    u = urllib.parse.urlsplit(url)
    if not u.query:
        return url
    q = [(k, R if _secret_name(k) else v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)]
    return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q)))


def redact_body(body, content_type=""):
    if body is None:
        return None
    try:
        return json.dumps(redact(json.loads(body)), ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    if "x-www-form-urlencoded" in (content_type or ""):
        q = urllib.parse.parse_qsl(body, keep_blank_values=True)
        return urllib.parse.urlencode([(k, R if _secret_name(k) else v) for k, v in q])
    return body


def write_private_json(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)
    return path


def _stamp():
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


# ----------------------------------------------------------------------------- browser + session
def open_penyedia(headed=True, keep_open=False, chrome_path=None, path="/products"):
    """Start (or reuse) the login profile's Chrome and return (browser, a penyedia tab)."""
    b = ic.Browser.start("chrome-penyedia", headed=headed, chrome_path=chrome_path, keep_open=keep_open)
    tabs = [t for t in b.tabs(PENYEDIA)]
    tab = tabs[0] if tabs else b.new_tab(PENYEDIA + path)
    tab.wait_load(timeout=60)
    return b, tab


def session_info(tab):
    """{signedIn, name, company, expires} for the profile's penyedia session (no tokens)."""
    url = tab.url()
    if not url.startswith(PENYEDIA):
        tab.navigate(PENYEDIA + "/products")
        tab.wait_load(timeout=60)
        url = tab.url()
    info = tab.evaluate(SESSION_JS) or {"signedIn": False}
    if not info.get("signedIn") and info.get("endpointStatus") == 404:
        # No session endpoint answered: fall back to where the site sent the tab.
        info["signedIn"] = url.startswith(PENYEDIA) and "/auth" not in url and "login" not in url
    return info


def _session_line(info):
    who = " · ".join(x for x in (info.get("name"), info.get("company")) if x) or "your account"
    exp = f" (session valid until {info['expires']})" if info.get("expires") else ""
    return f"Signed in to penyedia.inaproc.id as {who}{exp}."


def cmd_login(a):
    b, tab = open_penyedia(headed=True, chrome_path=a.chrome_path)
    try:
        print("A Chrome window is open on penyedia.inaproc.id. Sign in there yourself "
              "(the skill never sees your password). Waiting…", flush=True)
        deadline = time.time() + a.timeout
        while time.time() < deadline:
            if tab.closed:
                raise _err()("the login window was closed before signing in")
            try:
                info = session_info(tab)
            except ic.ChromeError:
                info = {"signedIn": False}
            if info.get("signedIn"):
                print(_session_line(info))
                return
            time.sleep(3)
        raise NotSignedIn(f"not signed in after {a.timeout:.0f}s; run `penyedia login` again")
    finally:
        b.close()


def cmd_status(a):
    b, tab = open_penyedia(headed=not a.headless, chrome_path=a.chrome_path)
    try:
        info = session_info(tab)
    finally:
        b.close()
    if a.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    if not info.get("signedIn"):
        raise NotSignedIn("not signed in to penyedia.inaproc.id; run `penyedia login`")
    if not a.json:
        print(_session_line(info))


def cmd_logout(a):
    b = ic.Browser.start("chrome-penyedia", headed=False, chrome_path=a.chrome_path)
    try:
        for origin in LOGOUT_ORIGINS:
            b.conn.send("Storage.clearDataForOrigin", {"origin": origin, "storageTypes": "all"})
    finally:
        b.close()
    print("Signed out: cookies and storage for *.inaproc.id were cleared from the skill's Chrome profile.")


# ----------------------------------------------------------------------------- recorder
class Recorder:
    """Records the site's own API traffic (all tabs and popups) while the owner works in the window."""

    KEEP_TYPES = {"Document", "XHR", "Fetch", "Other", "EventSource", "WebSocket", "Preflight"}

    def __init__(self, browser):
        self.b = browser
        self.entries = {}
        self.order = []
        self.tabs = {}

    @staticmethod
    def wanted(request, rtype):
        host = urllib.parse.urlsplit(request.get("url", "")).hostname or ""
        if any(host == t or host.endswith("." + t) for t in TRACKER_HOSTS):
            return False
        if request.get("method", "GET") != "GET":
            return True
        return rtype in Recorder.KEEP_TYPES

    def _watch(self, tab):
        if tab.target_id in self.tabs:
            return
        self.tabs[tab.target_id] = tab

        def sent(p):
            r = p.get("request", {})
            if not self.wanted(r, p.get("type")):
                return
            rid = p["requestId"]
            hdrs = r.get("headers", {})
            ctype = next((v for k, v in hdrs.items() if k.lower() == "content-type"), "")
            body = r.get("postData")
            if body is not None and "multipart/form-data" in ctype:
                body = _summarise_multipart(body, ctype)
            elif body is not None:
                body = redact_body(body, ctype)
            self.entries[rid] = {"time": _dt.datetime.now().isoformat(timespec="seconds"),
                                 "type": p.get("type"), "method": r.get("method"), "url": redact_url(r.get("url", "")),
                                 "requestHeaders": redact_headers(hdrs), "requestBody": body,
                                 "requestBodyOmitted": bool(r.get("hasPostData") and body is None)}
            self.order.append(rid)

        def got(p):
            e = self.entries.get(p["requestId"])
            if e is not None:
                resp = p.get("response", {})
                e.update(status=resp.get("status"), mimeType=resp.get("mimeType"),
                         responseHeaders=redact_headers(resp.get("headers", {})))

        def done(p):
            e = self.entries.get(p["requestId"])
            if e is None or e.get("method") == "OPTIONS":
                return
            mime = (e.get("mimeType") or "")
            if "json" in mime or "graphql" in mime or "text/plain" in mime:
                try:
                    r = tab.send("Network.getResponseBody", {"requestId": p["requestId"]}, timeout=10)
                    text = r.get("body", "")
                    if len(text) > 1024 * 1024:
                        e["responseBody"] = text[:1024 * 1024]
                        e["responseTruncated"] = True
                    else:
                        e["responseBody"] = redact_body(text, mime)
                except ic.ChromeError:
                    pass

        tab.on("Network.requestWillBeSent", sent)
        tab.on("Network.responseReceived", got)
        tab.on("Network.loadingFinished", done)
        tab.enable_network()

    def run(self, first_tab, duration_s=None):
        self._watch(first_tab)

        def created(params, sid):
            info = params.get("targetInfo", {})
            if info.get("type") == "page" and info.get("targetId") not in self.tabs:
                try:
                    self._watch(self.b._attach(info["targetId"]))
                except ic.ChromeError:
                    pass

        self.b.conn.on("Target.targetCreated", created)
        start = time.time()
        try:
            while self.b.alive and any(not t.closed for t in self.tabs.values()):
                if duration_s and time.time() - start > duration_s:
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        time.sleep(1)  # let in-flight response bodies arrive
        out = ic.private_dir("recordings") / f"penyedia_{_stamp()}.json"
        write_private_json(out, {"recordedAt": _dt.datetime.now().isoformat(timespec="seconds"),
                                 "site": PENYEDIA, "entries": [self.entries[r] for r in self.order]})
        return out

    def summary(self):
        rows = {}
        for rid in self.order:
            e = self.entries[rid]
            name = e["url"].split("?")[0]
            try:
                body = json.loads(e.get("requestBody") or "")
                if isinstance(body, dict) and body.get("operationName"):
                    name = "graphql " + body["operationName"]
            except (ValueError, TypeError):
                pass
            key = (name, e["method"])
            row = rows.setdefault(key, [name, e["method"], 0, set()])
            row[2] += 1
            row[3].add(str(e.get("status")))
        return [[n, m, c, ",".join(sorted(s))] for n, m, c, s in rows.values()]


def _summarise_multipart(body, ctype):
    m = re.search(r"boundary=([^;]+)", ctype)
    if not m:
        return f"<multipart, {len(body)} bytes>"
    parts = []
    for chunk in body.split("--" + m.group(1).strip('"')):
        head, _, content = chunk.partition("\r\n\r\n")
        name = re.search(r'name="([^"]*)"', head)
        if not name:
            continue
        fn = re.search(r'filename="([^"]*)"', head)
        ct = re.search(r"Content-Type:\s*(\S+)", head, re.I)
        parts.append({"name": name.group(1), "filename": fn.group(1) if fn else None,
                      "contentType": ct.group(1) if ct else None, "bytes": len(content.rstrip("\r\n-"))})
    return json.dumps({"multipart": parts})


def cmd_record(a):
    from inaproc import table
    b, tab = open_penyedia(headed=True, chrome_path=a.chrome_path)
    rec = Recorder(b)
    print("Recording penyedia.inaproc.id API calls. Work in the Chrome window (add, edit, delete a test "
          "product), then close the window to finish" + (f" (or wait {a.duration:g} min)." if a.duration else "."),
          flush=True)
    try:
        path = rec.run(tab, duration_s=a.duration * 60 if a.duration else None)
    finally:
        b.close()
    print(f"\nSaved {len(rec.order)} request(s) to {path} (tokens, cookies and signatures redacted).\n")
    print(table(["Operation / endpoint", "Method", "Count", "HTTP"], rec.summary()))


# ----------------------------------------------------------------------------- CLI wiring
def add_parsers(sub, common):
    p = sub.add_parser("penyedia", help="your own seller account on penyedia.inaproc.id (local Chrome needed)")
    ps = p.add_subparsers(dest="penyedia_cmd", required=True)

    def add(name, fn, help_):
        s = ps.add_parser(name, help=help_, parents=[common])
        s.set_defaults(fn=fn)
        s.add_argument("--chrome-path", help="Chrome/Chromium/Edge/Brave binary (default: found automatically)")
        return s

    s = add("login", cmd_login, "open a Chrome window and wait while you sign in yourself")
    s.add_argument("--timeout", type=float, default=600, help="seconds to wait for the sign-in (default 600)")
    s = add("status", cmd_status, "are you signed in? (exit code 3 when not)")
    s.add_argument("--headless", action="store_true", help="no window (may be refused by Cloudflare)")
    add("logout", cmd_logout, "clear the *.inaproc.id session from the skill's Chrome profile")
    s = add("record", cmd_record, "record the site's API calls while you work in the window")
    s.add_argument("--duration", type=float, help="stop after this many minutes (default: when the window closes)")
    return p
