"""Seller (penyedia) side of the inaproc skill: the owner's own login, an API recorder, and product commands.

Every call to penyedia.inaproc.id runs as fetch() inside a penyedia page of the skill's own Chrome profile
(`~/.inaproc/chrome-penyedia`), so cookies, token refresh and Cloudflare clearance stay in the browser.
The skill never sees, stores or types the password; the owner signs in in the Chrome window.
"""
import datetime as _dt
import hashlib
import json
import os
import platform
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

# Runs inside a penyedia page (synchronous, so it works in Chrome and Safari alike). Returns only what the
# CLI prints: never the raw session, which may hold tokens.
SESSION_JS = r"""(function(){
 try{
  var x=new XMLHttpRequest(); x.open('GET','/api/auth/session',false); x.setRequestHeader('accept','application/json');
  x.send(null);
  var j=null; try{ j=JSON.parse(x.responseText); }catch(e){}
  var u=(j&&j.user)||{};
  var pick=function(){for(var i=0;i<arguments.length;i++){var v=arguments[i];if(v!==undefined&&v!==null&&v!==''&&typeof v!=='object')return String(v);}return null;};
  return {signedIn: !!(j && (j.user || j.accessToken || j.expires)), endpointStatus: x.status,
          name: pick(u.name,u.fullName,u.fullname,u.username),
          company: pick(u.companyName,u.company&&u.company.name,u.company,u.sellerName,u.institutionName),
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
def default_browser():
    return os.environ.get("INAPROC_BROWSER") or ("safari" if platform.system() == "Darwin" else "chrome")


def open_penyedia(headed=True, keep_open=False, chrome_path=None, path="/products", browser=None):
    """Return (browser, a penyedia tab): the owner's own Safari, or the skill's Chrome profile."""
    if (browser or default_browser()) == "safari":
        import inaproc_safari
        sf = inaproc_safari.Safari()
        tab = sf.find_tab(PENYEDIA)
        if tab is None:
            tab = sf.new_tab(PENYEDIA + path)
        tab.wait_load(timeout=60)
        return sf, tab
    b = ic.Browser.start("chrome-penyedia", headed=headed, chrome_path=chrome_path, keep_open=keep_open)
    tabs = [t for t in b.tabs(PENYEDIA)]
    tab = tabs[0] if tabs else b.new_tab(PENYEDIA + path)
    tab.wait_load(timeout=60)
    return b, tab


def session_info(tab, navigate=True):
    """{signedIn, name, company, expires} for the profile's penyedia session (no tokens).

    navigate=False is for `login`: while the owner is on the sign-in pages (another origin, or
    penyedia's /auth pages) the tab is left alone, and the check waits until the site sends it back."""
    url = tab.url()
    on_auth = (not url.startswith(PENYEDIA)) or urllib.parse.urlsplit(url).path.startswith("/auth")
    if not navigate and on_auth:
        return {"signedIn": False, "pending": True}
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
    b, tab = open_penyedia(headed=True, chrome_path=a.chrome_path, browser=a.browser)
    try:
        where = "Safari" if a.browser == "safari" else "A Chrome window"
        print(f"{where} is open on penyedia.inaproc.id. Sign in there yourself if needed "
              "(the skill never sees your password). Waiting…", flush=True)
        deadline = time.time() + a.timeout
        while time.time() < deadline:
            if tab.closed:
                raise _err()("the login window was closed before signing in")
            try:
                info = session_info(tab, navigate=False)
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
    b, tab = open_penyedia(headed=not a.headless, chrome_path=a.chrome_path, browser=a.browser)
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
    if a.browser == "safari":
        print("You are using your own Safari: sign out there (penyedia.inaproc.id > your account > Keluar). "
              "The skill keeps no session of its own in Safari mode.")
        return
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
        return summarise([self.entries[r] for r in self.order])


def summarise(entries):
    rows = {}
    for e in entries:
        if True:
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


# Installed into the owner's Safari tab by `record`: logs the page's own fetch/XHR calls in window.__inaprocRec.
HOOK_JS = r"""(function(){
if(window.__inaprocRec)return 'ok';
var R=window.__inaprocRec=[];var MAX=1048576;
function hdrs(h){var o={};try{if(!h)return o;if(typeof h.forEach==='function'&&!Array.isArray(h)){h.forEach(function(v,k){o[k]=v;});}
 else if(Array.isArray(h)){h.forEach(function(p){o[p[0]]=p[1];});}else{for(var k in h)o[k]=h[k];}}catch(e){}return o;}
function bodyOf(b){if(b==null)return null;if(typeof b==='string')return b.length>MAX?b.slice(0,MAX):b;
 if(typeof FormData!=='undefined'&&b instanceof FormData){var parts=[];b.forEach(function(v,k){parts.push({name:k,
  filename:(v&&v.name)||null,contentType:(v&&v.type)||null,bytes:(v&&v.size!=null)?v.size:String(v).length});});
  return JSON.stringify({multipart:parts});}
 if(typeof Blob!=='undefined'&&b instanceof Blob)return '<blob '+b.type+' '+b.size+' bytes>';
 if(b instanceof ArrayBuffer)return '<binary '+b.byteLength+' bytes>';
 if(typeof URLSearchParams!=='undefined'&&b instanceof URLSearchParams)return b.toString();
 return '<'+Object.prototype.toString.call(b)+'>';}
function wantBody(ct){return /json|graphql|text\/plain/.test(ct||'');}
var of=window.fetch;
window.fetch=function(input,init){init=init||{};var url=typeof input==='string'?input:((input&&input.url)||String(input));
 var e={time:new Date().toISOString(),type:'Fetch',method:String(init.method||(input&&input.method)||'GET').toUpperCase(),
  url:new URL(url,location.href).href,requestHeaders:hdrs(init.headers||(input&&input.headers)),requestBody:bodyOf(init.body)};
 R.push(e);
 return of.apply(this,arguments).then(function(r){e.status=r.status;e.responseHeaders=hdrs(r.headers);
  var ct=r.headers.get('content-type')||'';e.mimeType=ct;
  if(wantBody(ct)){r.clone().text().then(function(t){e.responseBody=t.length>MAX?t.slice(0,MAX):t;e.responseTruncated=t.length>MAX;e.done=true;},function(){e.done=true;});}
  else{e.done=true;}return r;},function(err){e.error=String(err);e.done=true;throw err;});};
var XO=XMLHttpRequest.prototype.open,XS=XMLHttpRequest.prototype.send,XH=XMLHttpRequest.prototype.setRequestHeader;
XMLHttpRequest.prototype.open=function(m,u){this.__e={time:new Date().toISOString(),type:'XHR',method:String(m).toUpperCase(),
 url:new URL(u,location.href).href,requestHeaders:{}};return XO.apply(this,arguments);};
XMLHttpRequest.prototype.setRequestHeader=function(k,v){if(this.__e)this.__e.requestHeaders[k]=v;return XH.apply(this,arguments);};
XMLHttpRequest.prototype.send=function(b){var x=this,e=x.__e;if(e){e.requestBody=bodyOf(b);R.push(e);
 x.addEventListener('loadend',function(){e.status=x.status;var ct=x.getResponseHeader('content-type')||'';e.mimeType=ct;
  if(wantBody(ct)&&(x.responseType===''||x.responseType==='text')){var t=x.responseText||'';e.responseBody=t.length>MAX?t.slice(0,MAX):t;}
  e.done=true;});}
 return XS.apply(this,arguments);};
return 'installed';})()"""

DRAIN_JS = r"""(function(){var R=window.__inaprocRec;if(!R)return null;var out=[],keep=[];
R.forEach(function(e){if(e.done)out.push(e);else keep.push(e);});R.length=0;keep.forEach(function(e){R.push(e);});
return out;})()"""


def _clean_entry(e):
    hdrs = e.get("requestHeaders") or {}
    ctype = next((v for k, v in hdrs.items() if k.lower() == "content-type"), "")
    body = e.get("requestBody")
    if body is not None and not str(body).startswith(("<blob", "<binary", "{\"multipart\"")):
        body = redact_body(body, ctype)
    out = {"time": e.get("time"), "type": e.get("type"), "method": e.get("method"),
           "url": redact_url(e.get("url", "")), "requestHeaders": redact_headers(hdrs), "requestBody": body,
           "status": e.get("status"), "mimeType": e.get("mimeType"),
           "responseHeaders": redact_headers(e.get("responseHeaders") or {})}
    if e.get("responseBody") is not None:
        out["responseBody"] = redact_body(e["responseBody"], e.get("mimeType") or "")
    if e.get("responseTruncated"):
        out["responseTruncated"] = True
    if e.get("error"):
        out["error"] = e["error"]
    return out


class SafariRecorder:
    """`record` for Safari: a hook in the owner's penyedia tab logs the page's own fetch/XHR calls."""

    def __init__(self):
        self.entries = []

    def poll(self, tab):
        tab.evaluate(HOOK_JS)
        got = tab.evaluate(DRAIN_JS) or []
        self.entries += [_clean_entry(e) for e in got if not TRACKER_HOSTS or
                         not any(t in (urllib.parse.urlsplit(e.get("url", "")).hostname or "") for t in TRACKER_HOSTS)]

    def run(self, tab, duration_s=None):
        start = time.time()
        try:
            while not tab.closed:
                if duration_s and time.time() - start > duration_s:
                    break
                try:
                    self.poll(tab)
                except Exception as e:  # page mid-navigation; try again next second
                    if "Allow JavaScript" in str(e) or "control Safari" in str(e):
                        raise
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        try:
            self.poll(tab)
        except Exception:
            pass
        out = ic.private_dir("recordings") / f"penyedia_{_stamp()}.json"
        write_private_json(out, {"recordedAt": _dt.datetime.now().isoformat(timespec="seconds"),
                                 "site": PENYEDIA, "browser": "safari", "entries": self.entries})
        return out


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
    if a.browser == "safari":
        b, tab = open_penyedia(browser="safari")
        rec = SafariRecorder()
        print("Recording the penyedia.inaproc.id tab in Safari. Work in that tab (add, edit, delete a test product); "
              "stay in the same tab. Press Ctrl-C here, or close the tab, to finish"
              + (f" (or wait {a.duration:g} min)." if a.duration else "."), flush=True)
        path = rec.run(tab, duration_s=a.duration * 60 if a.duration else None)
        print(f"\nSaved {len(rec.entries)} request(s) to {path} (tokens, cookies and signatures redacted).\n")
        print(table(["Operation / endpoint", "Method", "Count", "HTTP"], summarise(rec.entries)))
        return
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


# ----------------------------------------------------------------------------- seller GraphQL client
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "penyedia_queries.json"), encoding="utf-8") as _f:
    ALLOWED_OPS = json.load(_f)  # the only seller operations this tool will ever send

ROOT_FIELD = {"getListProduct": "listProduct", "getProductById": "getProduct",
              "checkProductNameAvailability": "checkProductNameAvailability",
              "getAllProductCategoryListSeller": "getAllProductCategory",
              "requestUploadSignedUrl": "requestUploadSignedUrl", "createProduct": "createProduct",
              "createProductDraft": "createProductDraft", "updateProduct": "updateProduct",
              "updateProductDraft": "updateProductDraft", "deleteProduct": "deleteProduct",
              "updateProductActiveStatus": "updateProductActiveStatus"}


class SellerClient:
    """GraphQL calls to penyedia.inaproc.id, sent from inside the owner's signed-in tab, one at a time."""

    def __init__(self, tab, limiter=None):
        from inaproc import RateLimiter
        self.tab = tab
        self.limiter = limiter or RateLimiter(1.0)

    def gql(self, op, variables):
        InaprocError = _err()
        if op not in ALLOWED_OPS:
            raise InaprocError(f"{op} is not a product operation this tool may send")
        body = json.dumps({"operationName": op, "query": ALLOWED_OPS[op], "variables": variables})
        for attempt in range(3):
            self.limiter.wait()
            r = self.tab.page_fetch(GRAPHQL, "POST", {"content-type": "application/json",
                                                      "accept": "application/json"}, body)
            status, text = r.get("status"), r.get("text") or ""
            if status in (429, 502, 503, 504) and attempt < 2:
                time.sleep(5 * (attempt + 1) ** 2)
                continue
            break
        if status in (401, 403):
            raise NotSignedIn("penyedia.inaproc.id refused the call (signed out?); run `penyedia login`")
        try:
            doc = json.loads(text)
        except ValueError:
            raise InaprocError(f"{op}: HTTP {status}, not JSON: {text[:200]}")
        if doc.get("errors"):
            msgs = "; ".join(e.get("message", "?") for e in doc["errors"])
            if any("unauth" in m.lower() or "login" in m.lower() for m in msgs.split("; ")):
                raise NotSignedIn(f"{op}: {msgs}; run `penyedia login`")
            raise InaprocError(f"{op}: {msgs}")
        data = (doc.get("data") or {}).get(ROOT_FIELD[op])
        items = data if isinstance(data, list) else [data]
        for d in items:
            if isinstance(d, dict) and d.get("__typename") == "GenericError":
                raise InaprocError(f"{op}: {d.get('code')}: {d.get('message')} (reqId {d.get('reqId')})")
        return data

    def account(self):
        """'Name · Company' of the signed-in owner; raises NotSignedIn."""
        info = session_info(self.tab)
        if not info.get("signedIn"):
            raise NotSignedIn("not signed in to penyedia.inaproc.id; run `penyedia login`")
        return " · ".join(x for x in (info.get("name"), info.get("company")) if x) or "unknown"

    def list_products(self, page=1, per_page=20, filter=None):
        return self.gql("getListProduct", {"page": page, "perPage": per_page, "filter": filter or {}})

    def get_product(self, product_id):
        return self.gql("getProductById", {"id": str(product_id)})


# ----------------------------------------------------------------------------- plans, diff, apply, write log
def state_hash(product):
    return hashlib.sha256(json.dumps(product, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def diff(before, after, path=""):
    """[(path, old, new)] for every leaf that differs."""
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for k in list(before) + [k for k in after if k not in before]:
            out += diff(before.get(k), after.get(k), f"{path}.{k}" if path else str(k))
        return out
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        out = []
        for i, (x, y) in enumerate(zip(before, after)):
            out += diff(x, y, f"{path}[{i}]")
        return out
    return [] if before == after else [(path, before, after)]


def make_plan(kind, account, writes):
    plan = {"version": 1, "kind": kind, "account": account,
            "createdAt": _dt.datetime.now().astimezone().isoformat(timespec="seconds"), "writes": writes}
    path = ic.private_dir("penyedia") / f"plan_{_stamp()}_{kind}.json"
    n = 1
    while path.exists():
        n += 1
        path = ic.private_dir("penyedia") / f"plan_{_stamp()}_{kind}_{n}.json"
    write_private_json(path, plan)
    return path


def _log_write(entry):
    path = ic.private_dir("penyedia") / "writes.log.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.chmod(path, 0o600)


def apply_plan(client, path, continue_on_error=False):
    """Send exactly the writes in a plan file. Returns {done, failed, skipped, notRun, results}."""
    InaprocError = _err()
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    if plan.get("version") != 1 or not isinstance(plan.get("writes"), list):
        raise InaprocError(f"{path} is not a plan file made by this tool")
    account = client.account()  # raises NotSignedIn before anything is sent
    if plan.get("account") != account:
        raise InaprocError(f"this plan was made for {plan.get('account')!r}, but the signed-in account is "
                           f"{account!r}; nothing was sent")
    res = {"done": 0, "failed": 0, "skipped": 0, "notRun": 0, "results": []}
    stop = False
    for w in plan["writes"]:
        row = {"op": w["op"], "productId": w.get("productId"), "summary": w.get("summary")}
        res["results"].append(row)
        if stop:
            row["result"] = "not run (an earlier write failed)"
            res["notRun"] += 1
            continue
        if w.get("beforeHash"):
            try:
                current = client.get_product(w["productId"])
            except InaprocError as e:
                current, row["error"] = None, str(e)
            if current is None or state_hash(current) != w["beforeHash"]:
                row["result"] = "skipped: changed since dry run (make a new plan)"
                res["skipped"] += 1
                continue
        entry = {"time": _dt.datetime.now().astimezone().isoformat(timespec="seconds"), "plan": str(path),
                 "account": account, "op": w["op"], "productId": w.get("productId"),
                 "variables": redact(w.get("variables"))}
        try:
            response = client.gql(w["op"], w.get("variables") or {})
            row["result"] = "done"
            row["response"] = redact(response)
            res["done"] += 1
            entry.update(result="done", response=redact(response))
        except InaprocError as e:
            row["result"] = f"failed: {e}"
            res["failed"] += 1
            entry.update(result="failed", error=str(e))
            stop = not continue_on_error
        _log_write(entry)
    return res


def product_link(p):
    return f"https://katalog.inaproc.id/{p.get('username')}/{p.get('slug')}" if p.get("slug") else None


def product_rows(items):
    rows = []
    for p in items or []:
        pr = p.get("prices") or {}
        rows.append({"id": p.get("id"), "name": p.get("name"), "variants": len(p.get("variants") or []),
                     "minPrice": pr.get("minPrice"), "maxPrice": pr.get("maxPrice"),
                     "stock": p.get("stockAccumulation"), "status": p.get("status"), "link": product_link(p)})
    return rows


def _client(a):
    from inaproc import RateLimiter
    b, tab = open_penyedia(browser=a.browser, chrome_path=a.chrome_path, headed=True)
    return b, SellerClient(tab, RateLimiter(max(1.0, a.min_interval)))


def _money(v):
    return "" if v is None else f"Rp{float(v):,.0f}".replace(",", ".")


def cmd_products(a):
    from inaproc import emit, table
    b, c = _client(a)
    try:
        flt = json.loads(a.filter) if a.filter else {}
        res = c.list_products(page=a.page, per_page=a.per_page, filter=flt)
    finally:
        b.close()
    rows = product_rows(res.get("items"))
    if a.search:
        q = a.search.lower()
        rows = [r for r in rows if q in (r["name"] or "").lower()]
    if a.status:
        rows = [r for r in rows if (r["status"] or "").upper() == a.status.upper()]
    md = (f"**Your products** — page {res.get('currentPage')} of {res.get('lastPage')}, "
          f"{len(rows)} shown of {res.get('total')} total\n\n"
          + table(["ID", "Name", "Variants", "Price", "Stock", "Status", "Link"],
                  [[r["id"], r["name"], r["variants"],
                    _money(r["minPrice"]) + ("" if r["maxPrice"] in (None, r["minPrice"]) else "–" + _money(r["maxPrice"])),
                    r["stock"], r["status"], r["link"]] for r in rows]))
    emit(a, "penyedia-products", a.search or "all", {"page": res.get("currentPage"), "lastPage": res.get("lastPage"),
                                                     "total": res.get("total"), "items": rows}, md, {"products": rows})


def cmd_product_get(a):
    from inaproc import emit
    b, c = _client(a)
    try:
        p = c.get_product(a.id)
    finally:
        b.close()
    md = (f"**{p.get('name')}** (id {p.get('id')}, status {p.get('status')})\n\n```json\n"
          + json.dumps(p, ensure_ascii=False, indent=2) + "\n```")
    emit(a, "penyedia-product", str(a.id), p, md, {"product": [p]})


# ----------------------------------------------------------------------------- CLI wiring
def add_parsers(sub, common):
    p = sub.add_parser("penyedia", help="your own seller account on penyedia.inaproc.id (local Chrome needed)")
    ps = p.add_subparsers(dest="penyedia_cmd", required=True)

    def add(name, fn, help_):
        s = ps.add_parser(name, help=help_, parents=[common])
        s.set_defaults(fn=fn)
        s.add_argument("--browser", choices=["safari", "chrome"], default=default_browser(),
                       help="safari = your own Safari (default on macOS); chrome = the skill's Chrome profile")
        s.add_argument("--chrome-path", help="Chrome/Chromium/Edge/Brave binary (default: found automatically)")
        return s

    s = add("login", cmd_login, "open a Chrome window and wait while you sign in yourself")
    s.add_argument("--timeout", type=float, default=600, help="seconds to wait for the sign-in (default 600)")
    s = add("status", cmd_status, "are you signed in? (exit code 3 when not)")
    s.add_argument("--headless", action="store_true", help="no window (may be refused by Cloudflare)")
    add("logout", cmd_logout, "clear the *.inaproc.id session from the skill's Chrome profile")
    s = add("products", cmd_products, "list your products")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--per-page", type=int, default=20)
    s.add_argument("--search", help="only names containing this text (on the fetched page)")
    s.add_argument("--status", help="only this status, e.g. ACTIVE")
    s.add_argument("--filter", help="raw ListProductFilterInput JSON passed to the site")
    s = add("product", cmd_product_get, "show one of your products in full")
    s.add_argument("id")
    s = add("record", cmd_record, "record the site's API calls while you work in the window")
    s.add_argument("--duration", type=float, help="stop after this many minutes (default: when the window closes)")
    return p
