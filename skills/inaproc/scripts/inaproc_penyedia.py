"""Seller (penyedia) side of the inaproc skill: the owner's own login, an API recorder, and product commands.

Every call to penyedia.inaproc.id runs as fetch() inside a penyedia page of the skill's own Chrome profile
(`~/.inaproc/chrome-penyedia`), so cookies, token refresh and Cloudflare clearance stay in the browser.
The skill never sees, stores or types the password; the owner signs in in the Chrome window.
"""
import datetime as _dt
import hashlib
import sys
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
 if(location.origin!=='https://penyedia.inaproc.id'){return {signedIn:false, wrongOrigin:true, href:location.href};}
 try{
  var x=new XMLHttpRequest(); x.open('GET','/api/auth/session',false); x.setRequestHeader('accept','application/json');
  x.send(null);
  var j=null; try{ j=JSON.parse(x.responseText); }catch(e){}
  var u=(j&&j.user)||{};
  var pick=function(){for(var i=0;i<arguments.length;i++){var v=arguments[i];if(v!==undefined&&v!==null&&v!==''&&typeof v!=='object')return String(v);}return null;};
  return {signedIn: !!(j && (j.user || j.accessToken || j.expires)), endpointStatus: x.status, href:location.href,
          name: pick(u.name,u.fullName,u.fullname,u.username),
          company: pick(u.companyName,u.company&&u.company.name,u.company,u.sellerName,u.institutionName),
          expires: pick(j && j.expires)};
 }catch(e){ return {signedIn:false, error:String(e)}; }
})()"""


class NotSignedIn(Exception):
    exit_code = 3


class _TokenExpired(Exception):
    pass


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
def _progress(label):
    ui = sys.modules.get("inaproc_ui")
    if ui is not None:
        ui.progress(label)


def on_penyedia(url):
    u = urllib.parse.urlsplit(url or "")
    return u.scheme == "https" and u.hostname == "penyedia.inaproc.id"


def default_browser():
    return os.environ.get("INAPROC_BROWSER") or ("safari" if platform.system() == "Darwin" else "chrome")


def open_penyedia(headed=True, keep_open=False, chrome_path=None, path="/products", browser=None):
    """Return (browser, a penyedia tab): the owner's own Safari, or the skill's Chrome profile."""
    if (browser or default_browser()) == "safari":
        import inaproc_safari
        _progress("Mencari tab penyedia.inaproc.id di Safari")
        sf = inaproc_safari.Safari()
        tab = sf.find_tab(PENYEDIA)
        if tab is None:
            tab = sf.new_tab(PENYEDIA + path)
        tab.wait_load(timeout=60)
        return sf, tab
    b = ic.Browser.start("chrome-penyedia", headed=headed, chrome_path=chrome_path, keep_open=keep_open)
    tabs = [t for t in b.tabs(PENYEDIA) if on_penyedia(t.url())]
    tab = tabs[0] if tabs else b.new_tab(PENYEDIA + path)
    tab.wait_load(timeout=60)
    return b, tab


def session_info(tab, navigate=True):
    """{signedIn, name, company, expires} for the profile's penyedia session (no tokens).

    navigate=False is for `login`: while the owner is on the sign-in pages (another origin, or
    penyedia's /auth pages) the tab is left alone, and the check waits until the site sends it back."""
    if not navigate:
        url = tab.url()
        on_auth = (not on_penyedia(url)) or urllib.parse.urlsplit(url).path.startswith("/auth")
        if on_auth:
            return {"signedIn": False, "pending": True}
    _progress("Memeriksa sesi login penyedia")
    info = tab.evaluate(SESSION_JS) or {"signedIn": False}
    url = info.get("href") or tab.url()
    if navigate and not on_penyedia(url):
        tab.navigate(PENYEDIA + "/products")
        tab.wait_load(timeout=60)
        info = tab.evaluate(SESSION_JS) or {"signedIn": False}
        url = info.get("href") or tab.url()
    if not info.get("signedIn") and info.get("endpointStatus") == 404:
        # No session endpoint answered: fall back to where the site sent the tab.
        info["signedIn"] = on_penyedia(url) and "/auth" not in url and "login" not in url
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
            except Exception as e:  # page mid-load; Safari setup problems still stop the command
                if type(e).__name__ not in ("ChromeError", "SafariError") or "Allow JavaScript" in str(e) \
                        or "control Safari" in str(e):
                    raise
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
              "updateProductActiveStatus": "updateProductActiveStatus",
              "getAllProductCategoryListSearch": "allMinifiedProductCategory", "kbliByInstutionId": "getInstitutionByID",
              "OrderListMultishipment": "orderListV2", "getNegotiationList": "negotiationListV2",
              "orderDetailV2": "orderDetailV2", "negotiationDetailV3": "negotiationDetailV3"}
CATEGORY_BATCH = 50
WRITE_OPS = {"createProduct", "createProductDraft", "updateProduct", "updateProductDraft", "deleteProduct",
             "updateProductActiveStatus"}
NEEDS_BEFORE_HASH = {"updateProduct", "updateProductDraft", "deleteProduct", "updateProductActiveStatus"}


class SellerClient:
    """GraphQL calls to penyedia.inaproc.id, sent from inside the owner's signed-in tab, one at a time."""

    def __init__(self, tab, limiter=None):
        from inaproc import RateLimiter
        self.tab = tab
        self.limiter = limiter or RateLimiter(1.0)

    def gql(self, op, variables):
        """One GraphQL call. An expired access token (the login itself still valid) is refreshed by reloading the
        penyedia tab, which makes the site renew it, then the call is tried once more. A rejected call never ran,
        so retrying a write here cannot apply it twice."""
        try:
            return self._gql_once(op, variables)
        except _TokenExpired:
            _progress("Token penyedia kedaluwarsa · memuat ulang halaman")
            self.tab.navigate(PENYEDIA + "/products")
            self.tab.wait_load(timeout=60)
            time.sleep(1.5)
            try:
                return self._gql_once(op, variables)
            except _TokenExpired:
                raise NotSignedIn("sesi penyedia kedaluwarsa (ERR_EXPIRED_TOKEN). Buka penyedia.inaproc.id di Safari "
                                  "dan login lagi, atau jalankan `inaproc login`.")

    def _gql_once(self, op, variables):
        InaprocError = _err()
        if op not in ALLOWED_OPS:
            raise InaprocError(f"{op} is not a product operation this tool may send")
        body = json.dumps({"operationName": op, "query": ALLOWED_OPS[op], "variables": variables})
        _progress(f"Penyedia · {op}")
        # A 502/503/504 on a write may still have reached the origin: retrying could create duplicates.
        retry_on = (429,) if op in WRITE_OPS else (429, 502, 503, 504)
        for attempt in range(3):
            self.limiter.wait()
            r = self.tab.page_fetch(GRAPHQL, "POST", {"content-type": "application/json",
                                                      "accept": "application/json"}, body) or {}
            status, text = r.get("status"), r.get("text") or ""
            if status in retry_on and attempt < 2:
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
            if "EXPIRED_TOKEN" in msgs.upper() or "TOKEN EXPIRED" in msgs.upper():
                raise _TokenExpired(msgs)
            if any("unauth" in m.lower() or "login" in m.lower() for m in msgs.split("; ")):
                raise NotSignedIn(f"{op}: {msgs}; run `penyedia login`")
            raise InaprocError(f"{op}: {msgs}")
        if status != 200:
            raise InaprocError(f"{op}: HTTP {status}: {text[:200]}")
        data = (doc.get("data") or {}).get(ROOT_FIELD[op])
        items = data if isinstance(data, list) else [data]
        for d in items:
            if isinstance(d, dict) and d.get("__typename") == "GenericError":
                raise InaprocError(f"{op}: {d.get('code')}: {d.get('message')} (reqId {d.get('reqId')})")
        if op in WRITE_OPS and not (isinstance(data, dict) and (
                data.get("id") or data.get("__typename") in ("Product", "StatusResponse"))):
            raise InaprocError(f"{op}: the site returned no result ({json.dumps(data)[:120]}); check "
                               "`penyedia products` before trying again")
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

    def category_tree(self):
        return (self.gql("getAllProductCategoryListSearch", {"input": {}}) or {}).get("items") or []

    def category_kbli(self, ids):
        """{category id: {kbli, kbliCodes, isActive}} via read-only aliased getProductCategory lookups, 50 a request."""
        out = {}
        fields = "... on ProductCategory{id kbli kbliCodes{code version} isActive}"
        for start in range(0, len(ids), CATEGORY_BATCH):
            chunk = ids[start:start + CATEGORY_BATCH]
            _progress(f"Membaca KBLI kategori {start + len(chunk)}/{len(ids)}")
            q = "query CategoryKbliBatch{" + " ".join(
                f"c{i}:getProductCategory(id:{json.dumps(str(cid))}){{{fields}}}" for i, cid in enumerate(chunk)) + "}"
            self.limiter.wait()
            r = self.tab.page_fetch(GRAPHQL, "POST", {"content-type": "application/json", "accept": "application/json"},
                                    json.dumps({"operationName": "CategoryKbliBatch", "query": q}))
            if r.get("status") in (401, 403):
                raise NotSignedIn("penyedia.inaproc.id refused the call (signed out?); run `penyedia login`")
            try:
                data = json.loads(r.get("text") or "").get("data") or {}
            except ValueError:
                raise _err()(f"category lookup: HTTP {r.get('status')}")
            for i, cid in enumerate(chunk):
                d = data.get(f"c{i}") or {}
                out[cid] = {"kbli": d.get("kbli"), "kbliCodes": d.get("kbliCodes"), "isActive": d.get("isActive")}
        return out

    def company_kbli(self, seller_id):
        inst = self.gql("kbliByInstutionId", {"id": seller_id}) or {}
        seen, out = set(), []
        for proj in ((inst.get("company") or {}).get("oss") or {}).get("project") or []:
            for k in proj.get("kbli") or []:
                key = (k.get("kbli"), k.get("versi"))
                if k.get("kbli") and key not in seen:
                    seen.add(key)
                    out.append({"code": k["kbli"], "name": k.get("name"), "version": k.get("versi")})
        return out

    def seller_id(self):
        path = ic.private_dir("penyedia") / "account.json"
        try:
            with open(path, encoding="utf-8") as f:
                sid = json.load(f).get("sellerId")
            if sid:
                return sid
        except (OSError, ValueError):
            pass
        items = (self.list_products(page=1, per_page=1) or {}).get("items") or []
        if not items:
            raise _err()("could not find your seller id (no products yet); pass --seller-id")
        sid = self.get_product(items[0]["id"]).get("sellerId")
        if sid:
            write_private_json(path, {"sellerId": sid})
        return sid

    def name_available(self, name):
        r = self.gql("checkProductNameAvailability", {"name": name, "productID": None})
        return bool((r or {}).get("available", True))


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


def _plan_key():
    path = ic.private_dir("penyedia") / ".plan_key"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(32))
    return path.read_bytes()


def _plan_digest(plan):
    import hmac
    body = json.dumps({"kind": plan.get("kind"), "account": plan.get("account"), "writes": plan.get("writes")},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hmac.new(_plan_key(), body, hashlib.sha256).hexdigest()


def make_plan(kind, account, writes):
    if not account or account == "unknown":
        raise _err()("could not tell which account is signed in; refusing to make a plan")
    for w in writes:
        if w["op"] in NEEDS_BEFORE_HASH and not w.get("beforeHash"):
            raise _err()(f"{w['op']} needs the product's state at dry-run time")
    plan = {"version": 1, "kind": kind, "account": account,
            "createdAt": _dt.datetime.now().astimezone().isoformat(timespec="seconds"), "writes": writes}
    plan["digest"] = _plan_digest(plan)
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
    import hmac
    if not hmac.compare_digest(str(plan.get("digest", "")), _plan_digest(plan)):
        raise InaprocError("this plan was changed after the dry run (or was not made on this computer); "
                           "nothing was sent. Make a new dry run.")
    if plan.get("appliedAt"):
        raise InaprocError(f"this plan was already applied at {plan['appliedAt']}; nothing was sent. "
                           "Make a new dry run if you want to send it again.")
    for w in plan["writes"]:
        if w.get("op") in NEEDS_BEFORE_HASH and not w.get("beforeHash"):
            raise InaprocError(f"{w.get('op')} in this plan has no dry-run state to check against; nothing was sent")
    account = client.account()  # raises NotSignedIn before anything is sent
    if plan.get("account") != account:
        raise InaprocError(f"this plan was made for {plan.get('account')!r}, but the signed-in account is "
                           f"{account!r}; nothing was sent")
    res = {"done": 0, "failed": 0, "skipped": 0, "notRun": 0, "results": []}
    stop = False
    plan["appliedAt"] = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    write_private_json(path, plan)  # marked before sending: a plan is never sent twice
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
        _log_write(dict(entry, result="sending"))
        interrupted = False
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
        except (Exception, KeyboardInterrupt) as e:
            interrupted = isinstance(e, KeyboardInterrupt)
            msg = "interrupted" if interrupted else str(e)
            row["result"] = (f"failed (outcome unknown: {msg}); check `penyedia products` before making "
                             "a new plan")
            res["failed"] += 1
            entry.update(result="failed (outcome unknown)", error=msg)
            stop = True
        entry["time"] = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
        _log_write(entry)
        if interrupted:
            for rest in plan["writes"][len(res["results"]):]:
                res["results"].append({"op": rest["op"], "productId": rest.get("productId"),
                                       "summary": rest.get("summary"), "result": "not run (interrupted)"})
                res["notRun"] += 1
            break
    return res


# ----------------------------------------------------------------------------- product -> GraphQL input
# Mirrors the site's own form converters (penyedia bundle module 32426: B6, H3, Mk, tM, jH, D7, Ad, AB, FU, ct).
_TO_GR = {"GR": 1, "KG": 1000}
_TO_CM = {"CM": 1, "M": 100, "MM": 0.1}


def _num(v):
    return int(v) if isinstance(v, float) and v.is_integer() else v


def _drop_none(d):
    return {k: v for k, v in d.items() if v is not None}


def _shipping(p):
    if p.get("type") != "PHYSICAL":
        return None
    sh = p.get("shipping") or {}
    wf = _TO_GR.get((sh.get("weightUnit") or "GR").upper(), 1)
    df = _TO_CM.get((sh.get("dimensionUnit") or "CM").upper(), 1)
    return {"width": _num((sh.get("width") or 0) * df), "weight": _num((sh.get("weight") or 0) * wf),
            "length": _num((sh.get("length") or 0) * df), "height": _num((sh.get("height") or 0) * df),
            "hasSellerShipping": bool(sh.get("hasSellerShipping")), "dimensionUnit": "CM", "weightUnit": "GR"}


def _infos(items, keep_id):
    out = []
    for e in items or []:
        if e.get("isReadOnly"):
            continue
        row = {"customFormFieldId": e.get("customFormFieldId"), "name": e.get("name"), "value": e.get("value")}
        if keep_id:
            row["id"] = e.get("id")
        out.append(row)
    return out


def _common_input(p, keep_ids):
    phys = p.get("type") == "PHYSICAL"
    pdn = p.get("pdn") or {}
    tax = p.get("tax") or {}
    tkdn, sni, brand = p.get("tkdn") or {}, p.get("sni"), p.get("brand") or {}
    stock_unit = p.get("stockUnit") or {}
    prices = p.get("prices") or {}
    inp = {
        "name": p.get("name") or "", "description": p.get("description") or "",
        "category": {"id": (p.get("category") or {}).get("id") or ""}, "kbki": p.get("kbki") or "",
        "pdn": {"type": pdn.get("type"), "laborType": pdn.get("laborType"), "locationType": pdn.get("locationType"),
                "materialType": pdn.get("materialType"), "countryCode": pdn.get("countryCode")},
        "isActive": p.get("isActive", True),
        "images": [_drop_none({"id": i.get("id") if keep_ids else None, "imageUrl": i.get("imageUrl")})
                   for i in p.get("images") or [] if not i.get("isReadOnly")],
        "videoSource": p.get("videoSource"), "videoUrl": p.get("videoUrl"),
        "productInformations": {
            "additionalInformations": _infos((p.get("productInformations") or {}).get("additionalInformations"), keep_ids),
            "mainInformations": _infos((p.get("productInformations") or {}).get("mainInformations"), keep_ids)},
        "productAddOns": None if p.get("productAddOns") is None else [
            _drop_none({"id": a.get("id") if keep_ids else None, "name": a.get("name"), "description": a.get("description"),
                        "type": a.get("type"), "productAddOnVariants": [
                            _drop_none({"id": v.get("id") if keep_ids else None, "name": v.get("name"),
                                        "description": v.get("description"), "price": v.get("price")})
                            for v in a.get("productAddOnVariants") or []]})
            for a in p["productAddOns"]],
        "productOptions": None if p.get("productOptions") is None else [
            _drop_none({"id": o.get("id") if keep_ids else None, "option": o.get("option"),
                        "optionLevel": o.get("optionLevel"), "values": o.get("values") or [],
                        "valueIds": o.get("valueIds") if keep_ids else None})
            for o in p["productOptions"]],
        "variants": [dict(_drop_none({"id": v.get("id") if keep_ids else None}), **{
            "price": v.get("price") or 0, "sku": v.get("sku") or "", "stock": v.get("stock") or 0,
            "options": v.get("options"), "optionValues": v.get("optionValues"),
            "isActive": v.get("isActive", True) is not False, "imageUrl": v.get("imageUrl") or ""})
            for v in p.get("variants") or []],
        "prices": {"minPurchase": prices.get("minPurchase") or 1, "productWholesalePrices": [
            _drop_none({"id": w.get("id") if keep_ids else None, "minQuantity": w.get("minQuantity") or 0,
                        "price": w.get("price") or 0}) for w in prices.get("productWholesalePrices") or []]},
        "tkdn": {"number": tkdn["number"], "specificationId": tkdn.get("specificationId")}
        if tkdn.get("number") and tkdn.get("status") not in ("REJECTED", "EXPIRED") else None,
        "sni": {"brand": sni.get("brand") or "", "certificateNumber": sni.get("certificateNumber") or "",
                "sniNumber": sni.get("sniNumber") or "", "spptNumber": sni.get("spptNumber"),
                "companyName": sni.get("companyName")} if sni else None,
        "brand": {"applicationNumber": brand["applicationNumber"]} if brand.get("applicationNumber") else None,
        "preOrder": {"sla": (p.get("preOrder") or {}).get("sla") or 0} if p.get("preOrder") else None,
        "stockUnit": {"primaryUnit": stock_unit.get("primaryUnit") or "PCS", "value": stock_unit.get("value"),
                      "secondaryUnit": stock_unit.get("secondaryUnit")},
        "shipping": _shipping(p),
        "shippingOption": (p.get("shippingOption") or "STANDARD") if phys else "NO_SHIPMENT",
        "tax": _drop_none({"ppnPercentage": tax.get("ppnPercentage"), "ppnBmId": tax.get("ppnBmId"),
                           "ppnBmTaxPaymentStatus": tax.get("ppnBmTaxPaymentStatus") if tax.get("ppnBmId") else None,
                           "ppnBmTaxPaymentFileToken": tax.get("ppnBmTaxPaymentFileToken") if tax.get("ppnBmId") else None}),
        "additionalFee": {"isActive": (p.get("additionalFee") or {}).get("isActive")} if p.get("additionalFee") else None,
        "hasDecimalQuantity": bool(p.get("hasDecimalQuantity")),
    }
    return {k: v for k, v in inp.items() if v is not None}


def _parse_amount(v, what):
    if isinstance(v, (int, float)):
        return _num(v)
    t = str(v).strip().replace("Rp", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    t = t.replace(",", ".")
    try:
        n = float(t)
    except ValueError:
        raise _err()(f"{what} {v!r} is not a number")
    if n < 0:
        raise _err()(f"{what} cannot be negative")
    return _num(n)


def _apply_variant_changes(inp, product, changes):
    for vc in changes.get("variantChanges") or []:
        _apply_one_variant_change(inp, product, vc)
    _apply_one_variant_change(inp, product, changes)


def _apply_one_variant_change(inp, product, changes):
    price, stock = changes.get("price"), changes.get("stock")
    if price is None and stock is None:
        return
    variants = inp["variants"]
    want = changes.get("variant")
    if want:
        idx = [i for i, v in enumerate(product.get("variants") or []) if want in (v.get("id"), v.get("sku"))]
        if not idx:
            raise _err()(f"variant {want!r} not found in product {product.get('id')}")
    elif len(variants) == 1:
        idx = [0]
    else:
        raise _err()(f"product {product.get('id')} has {len(variants)} variants; pass --variant ID_OR_SKU")
    for i in idx:
        if price is not None:
            variants[i]["price"] = _parse_amount(price, "price")
        if stock is not None:
            variants[i]["stock"] = int(_parse_amount(stock, "stock"))


def clone_input(product, name, price=None, stock=None, draft=False, images=True):
    """CreateProductInput (draft=False) or CreateProductDraftInput (draft=True) for a new product copied from one
    of the owner's products. The draft input has no isActive (the site's draft form leaves it out)."""
    inp = _common_input(product, keep_ids=False)
    inp["name"] = name
    if draft:
        inp.pop("isActive", None)
    if not images:
        inp["images"] = []  # the site does not let a new product reuse another product's image files
    if inp.get("shippingOption") not in ("NO_SHIPMENT", "SELLER_COURIER"):
        inp["logisticConfigurations"] = []
    _apply_variant_changes(inp, product, {"price": price, "stock": stock})
    return inp


def update_input(product, changes):
    """UpdateProductInput: the product as it is (ids kept) with `changes` applied.

    changes: price, stock, variant (id or sku), name, set {dotted.path: value}."""
    inp = _common_input(product, keep_ids=True)
    if changes.get("name"):
        inp["name"] = changes["name"]
    _apply_variant_changes(inp, product, changes)
    for path, value in (changes.get("set") or {}).items():
        node, keys = inp, path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
    return inp


def delete_writes(client, ids):
    writes = []
    for pid in ids:
        p = client.get_product(pid)
        writes.append({"op": "deleteProduct", "variables": {"id": str(pid)}, "productId": str(pid),
                       "beforeHash": state_hash(p),
                       "summary": f"DELETE {p.get('name')} (status {p.get('status')}) {product_link(p) or ''}".strip()})
    return writes


def update_writes(client, pid, changes):
    p = client.get_product(pid)
    before, after = update_input(p, {}), update_input(p, changes)
    changed = diff(before, after)
    if not changed:
        return [], p, []
    return [{"op": "updateProduct", "variables": {"id": str(pid), "input": after}, "productId": str(pid),
             "beforeHash": state_hash(p), "summary": f"UPDATE {p.get('name')}",
             "diff": [[path, old, new] for path, old, new in changed]}], p, changed


def bulk_writes(client, rows):
    """rows: dicts with id plus any of price, stock, name, variant. Rows for the same product (e.g. one per
    variant) are merged into one update. Returns (writes, skipped, invalid)."""
    writes, skipped, invalid = [], [], []
    groups, order = {}, []
    for n, row in enumerate(rows, 1):
        row = {str(k).strip().lower(): ("" if v is None else str(v).strip()) for k, v in row.items()}
        pid = row.get("id")
        if not pid:
            invalid.append(f"row {n}: no id")
            continue
        try:
            for k in ("price", "stock"):
                if row.get(k):
                    _parse_amount(row[k], k)
        except _err() as e:
            invalid.append(f"row {n} ({pid}): {e}")
            continue
        if pid not in groups:
            groups[pid] = {"rows": [], "changes": {"variantChanges": []}}
            order.append(pid)
        g = groups[pid]
        g["rows"].append(n)
        if row.get("name"):
            g["changes"]["name"] = row["name"]
        vc = {k: row[k] for k in ("price", "stock", "variant") if row.get(k)}
        if vc.get("price") or vc.get("stock"):
            g["changes"]["variantChanges"].append(vc)
    for pid in order:
        g = groups[pid]
        label = "row " + ",".join(map(str, g["rows"]))
        try:
            w, _, _ = update_writes(client, pid, g["changes"])
        except _err() as e:
            invalid.append(f"{label} ({pid}): {e}")
            continue
        if w:
            writes += w
        else:
            skipped.append(f"{label} ({pid}): no change")
    return writes, skipped, invalid


# ----------------------------------------------------------------------------- categories eligible for the KBLI
def kbli_eligible(category_kbli, seller_codes):
    """The site's own rule (penyedia bundle, module 22350 `Np`): no KBLI list -> any KBLI will do;
    otherwise one of the category's codes must be among the company's codes."""
    if category_kbli is None:
        return len(seller_codes) > 0
    return any(code in seller_codes for code in category_kbli)


def leaf_categories(items):
    out = []

    def walk(node, path):
        kids = node.get("children") or []
        here = path + [node.get("name") or ""]
        if not kids:
            out.append({"id": node.get("id"), "path": " > ".join(here), "levels": here,
                        "productType": node.get("productType")})
        for k in kids:
            walk(k, here)

    for it in items:
        walk(it, [])
    return out


def _category_cache(client, refresh):
    path = ic.private_dir("cache") / "penyedia_category_kbli.json"
    if not refresh and path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
        with open(path, encoding="utf-8") as f:
            return json.load(f), True
    leaves = leaf_categories(client.category_tree())
    kb = client.category_kbli([c["id"] for c in leaves])
    for c in leaves:
        c.update(kb.get(c["id"]) or {})
    write_private_json(path, leaves)
    return leaves, False


TYPE_NAMES = {"PHYSICAL": "barang", "SERVICE": "jasa", "DIGITAL": "digital"}


def cmd_categories(a):
    from inaproc import emit, table
    b, c = _client(a)
    try:
        seller = a.seller_id or c.seller_id()
        company = c.company_kbli(seller)
        cats, cached = _category_cache(c, a.refresh)
    finally:
        b.close()
    codes = {k["code"] for k in company}
    rows = []
    for cat in cats:
        if cat.get("isActive") is False:
            continue
        ok = kbli_eligible(cat.get("kbli"), codes)
        if not (ok or a.all):
            continue
        if a.type and TYPE_NAMES.get(cat.get("productType")) != a.type:
            continue
        if a.search and a.search.lower() not in cat["path"].lower():
            continue
        match = sorted(codes & set(cat.get("kbli") or [])) if cat.get("kbli") is not None else ["(any)"]
        levels = (cat.get("levels") or cat["path"].split(" > ")) + ["", ""]
        rows.append({"level1": levels[0], "level2": levels[1], "level3": " > ".join(x for x in levels[2:] if x),
                     "id": cat["id"], "category": cat["path"], "type": TYPE_NAMES.get(cat.get("productType"), ""),
                     "eligible": ok, "matchingKbli": ", ".join(match), "categoryKbli": ", ".join(cat.get("kbli") or [])})
    total = sum(1 for cat in cats if cat.get("isActive") is not False)
    n_ok = sum(1 for cat in cats if cat.get("isActive") is not False and kbli_eligible(cat.get("kbli"), codes))
    head = (f"**Categories you can list in** — {n_ok} of {total} active categories match your KBLI "
            f"({', '.join(sorted(codes)) or 'none'})" + (" · from cache (`--refresh` to re-read)" if cached else ""))
    cols = ["Level 1", "Level 2", "Level 3", "Type", "Your matching KBLI", "ID"] + (["Eligible"] if a.all else [])
    md = head + f"\n\n{len(rows)} shown\n\n" + table(
        cols, [[r["level1"], r["level2"], r["level3"], r["type"], r["matchingKbli"], r["id"]]
               + (["yes" if r["eligible"] else "no"] if a.all else []) for r in rows], width=120)
    emit(a, "penyedia-categories", a.search or ("all" if a.all else "eligible"),
         {"companyKbli": company, "eligibleCount": n_ok, "total": total, "items": rows, "tree": category_tree_of(rows)},
         md, {"categories": rows, "company_kbli": company})


def category_tree_of(rows):
    """Level 1 > Level 2 > Level 3 nesting of category rows (for JSON/YAML)."""
    tree, l1s = [], {}
    for r in rows:
        n1 = l1s.get(r["level1"])
        if n1 is None:
            n1 = l1s[r["level1"]] = {"name": r["level1"], "children": [], "_l2": {}}
            tree.append(n1)
        n2 = n1["_l2"].get(r["level2"])
        if n2 is None:
            n2 = n1["_l2"][r["level2"]] = {"name": r["level2"], "children": []}
            n1["children"].append(n2)
        leaf = {"name": r["level3"], "id": r["id"], "type": r["type"], "matchingKbli": r["matchingKbli"]}
        if "eligible" in r:
            leaf["eligible"] = r["eligible"]
        n2["children"].append(leaf)
    for n1 in tree:
        n1.pop("_l2")
    return tree


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


def _save_plan_and_tell(client, kind, writes, lines):
    plan = make_plan(kind, client.account(), writes)
    print("\n".join(lines))
    print(f"\nNothing was sent. To send exactly this plan: penyedia apply {plan}")
    return plan


def cmd_create(a):
    InaprocError = _err()
    op = "createProduct" if a.submit else "createProductDraft"
    b, c = _client(a)
    try:
        if a.spec:
            with open(a.spec, encoding="utf-8") as f:
                inp = json.load(f)
            src_line = f"from spec {a.spec}"
        else:
            if not (a.from_id and a.name):
                raise InaprocError("give --from PRODUCT_ID and --name NEW_NAME (or a SPEC.json)")
            src = c.get_product(a.from_id)
            inp = clone_input(src, a.name, price=a.price, stock=a.stock, draft=not a.submit,
                              images=not a.no_images)
            src_line = f"cloned from {src.get('name')} ({a.from_id})"
        name = inp.get("name") or ""
        if len(name) < 5:
            raise InaprocError("the product name needs at least 5 characters")
        if not c.name_available(name):
            raise InaprocError(f"the name {name!r} is already used by one of your products")
        v = inp.get("variants") or [{}]
        lines = [f"**Dry run: {'create and submit for curation' if a.submit else 'create as DRAFT'}** — {op}",
                 f"- Name: {name}", f"- {src_line}",
                 f"- Category: {(inp.get('category') or {}).get('id')}",
                 f"- Variants: {len(v)}; first price {_money(v[0].get('price'))}, stock {v[0].get('stock')}",
                 f"- Images: {len(inp.get('images') or [])}"]
        lines += ["", "Full payload that `apply` will send:", "```json",
                  json.dumps(inp, ensure_ascii=False, indent=2), "```"]
        _save_plan_and_tell(c, "create", [{"op": op, "variables": {"input": inp}, "summary": f"CREATE {name}"}],
                            lines)
    finally:
        b.close()


def cmd_update(a):
    from inaproc import table
    changes = {k: v for k, v in (("price", a.price), ("stock", a.stock), ("name", a.name),
                                 ("variant", a.variant)) if v is not None}
    if a.set:
        changes["set"] = {}
        for item in a.set:
            k, _, v = item.partition("=")
            try:
                changes["set"][k] = json.loads(v)
            except ValueError:
                changes["set"][k] = v
    b, c = _client(a)
    try:
        writes, p, changed = update_writes(c, a.id, changes)
        if not writes:
            print(f"No change: {p.get('name')} already has these values. Nothing to send.")
            return
        lines = [f"**Dry run: update {p.get('name')}** ({product_link(p) or a.id})", "",
                 table(["Field", "Now", "After"], [[k, json.dumps(o, ensure_ascii=False), json.dumps(n, ensure_ascii=False)]
                                                  for k, o, n in changed]),
                 "", "Full payload: in the plan file below (every other field is sent as the site has it now)."]
        _save_plan_and_tell(c, "update", writes, lines)
    finally:
        b.close()


def cmd_delete(a):
    b, c = _client(a)
    try:
        writes = delete_writes(c, a.ids)
        lines = [f"**Dry run: delete {len(writes)} product(s)** — this cannot be undone on the site:"]
        lines += [f"- {w['summary']}" for w in writes]
        _save_plan_and_tell(c, "delete", writes, lines)
    finally:
        b.close()


def _read_rows(path):
    import csv
    if path.lower().endswith(".xlsx"):
        from inaproc import read_xlsx
        sheets = read_xlsx(path)
        grid = next(iter(sheets.values())) if sheets else []
        if not grid:
            return []
        head = [str(h or "").strip() for h in grid[0]]
        return [dict(zip(head, r)) for r in grid[1:] if any(c not in (None, "") for c in r)]
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def cmd_bulk(a):
    from inaproc import table
    b, c = _client(a)
    try:
        writes, skipped, invalid = bulk_writes(c, _read_rows(a.file))
        if invalid:
            raise _err()("fix these rows first (no plan was made):\n" + "\n".join(f"- {x}" for x in invalid))
        if not writes:
            print("Every row matches the site already. Nothing to send.")
            return
        rows = [[w["productId"], w["summary"][7:], p, json.dumps(o), json.dumps(n)]
                for w in writes for p, o, n in w["diff"]]
        lines = [f"**Dry run: bulk update, {len(writes)} product(s)** ({len(skipped)} unchanged row(s) skipped)", "",
                 table(["ID", "Product", "Field", "Now", "After"], rows)]
        _save_plan_and_tell(c, "bulk-update", writes, lines)
    finally:
        b.close()


def cmd_apply(a):
    from inaproc import table
    b, c = _client(a)
    try:
        res = apply_plan(c, a.plan, continue_on_error=a.continue_on_error)
    finally:
        b.close()
    print(table(["Operation", "Product", "What", "Result"],
                [[r["op"], r.get("productId") or (r.get("response") or {}).get("id") or "", r.get("summary"),
                  r["result"]] for r in res["results"]]))
    for r in res["results"]:
        if r["result"].startswith("failed"):
            print(f"\n{r['op']} {r.get('productId') or ''}: {r['result'][len('failed: '):]}")
    print(f"\n{res['done']} done, {res['failed']} failed, {res['skipped']} skipped, {res['notRun']} not run. "
          f"Logged to {ic.private_dir('penyedia') / 'writes.log.jsonl'}")
    if res["failed"]:
        raise _err()("some writes failed (see above)")


# ----------------------------------------------------------------------------- orders & negotiations (read-only)
STATUS_ID = {
    "COMPLETED": "Selesai", "DONE": "Selesai", "FINISHED": "Selesai", "CANCELLED": "Dibatalkan",
    "CANCELLED_ON_NEGOTIATION": "Batal saat negosiasi", "EXPIRED": "Kedaluwarsa", "REJECTED": "Ditolak",
    "ON_NEGOTIATION": "Negosiasi berjalan", "NEGOTIATION": "Negosiasi berjalan", "IN_NEGOTIATION": "Negosiasi berjalan",
    "WAITING_SELLER_RESPONSE": "Menunggu respons penyedia", "WAITING_BUYER_RESPONSE": "Menunggu respons pembeli",
    "WAITING_CONFIRMATION": "Menunggu konfirmasi", "WAITING_PAYMENT": "Menunggu pembayaran", "PAID": "Sudah dibayar",
    "PROCESSING": "Diproses", "ON_PROCESS": "Diproses", "SHIPPED": "Dikirim", "ON_DELIVERY": "Dalam pengiriman",
    "DELIVERED": "Diterima", "ACCEPTED": "Disetujui", "AGREED": "Disepakati", "DEAL": "Disepakati",
}
ROLE_ID = {"BUYER": "Pembeli", "SELLER": "Penyedia", "PPK": "PPK", "PP": "PP"}


def status_label(raw):
    if not raw:
        return "-"
    return STATUS_ID.get(raw) or raw.replace("_", " ").capitalize()


def _date(ts):
    return (ts or "")[:16].replace("T", " ") or "-"


def _matches_status(raw, want):
    w = (want or "").strip().lower()
    return not w or w == "all" or w in (raw or "").lower() or w in status_label(raw).lower()


def _paged(client, op, root_page, per_page, all_pages, page):
    pages, out, last, total = ([page] if not all_pages else range(1, 51)), [], page, 0
    for pg in pages:
        d = client.gql(op, {"input": {"pagination": {"page": pg, "perPage": per_page}}}) or {}
        out += d.get("items") or []
        last, total = d.get("lastPage") or pg, d.get("total") or len(out)
        if not all_pages or pg >= last:
            break
    return out, total, last


def _find(items, ref):
    ref = str(ref).strip()
    for it in items:
        if ref in (it.get("id"), it.get("orderNumber"), it.get("orderId"), it.get("orderKey")) or \
                (it.get("orderNumber") or "").endswith(ref):
            return it
    return None


def _lookup(client, op, ref):
    for pg in range(1, 21):
        d = client.gql(op, {"input": {"pagination": {"page": pg, "perPage": 50}}}) or {}
        hit = _find(d.get("items") or [], ref)
        if hit or pg >= (d.get("lastPage") or pg):
            return hit
    return None


def order_rows(items):
    rows = []
    for o in items:
        fi = o.get("firstItem") or {}
        snap = fi.get("snapshot") or {}
        ship = (o.get("shipments") or [{}])[0] or {}
        rows.append({"orderNumber": o.get("orderNumber"), "date": _date(o.get("timestamp")),
                     "status": status_label(o.get("status")), "statusCode": o.get("status"),
                     "buyer": ((o.get("buyer") or {}).get("buyerSnapshot") or {}).get("institutionName"),
                     "product": snap.get("name"), "qty": fi.get("qty"),
                     "unit": (snap.get("stockUnit") or {}).get("primaryUnit"), "items": o.get("rowItemCount"),
                     "total": o.get("total"), "viaNegotiation": bool(o.get("viaNegotiation")),
                     "courier": " ".join(x for x in (ship.get("provider"), ship.get("service")) if x) or None,
                     "id": o.get("id"), "orderKey": o.get("orderKey")})
    return rows


def nego_rows(items):
    rows = []
    for n in items:
        fi = n.get("firstItem") or {}
        rows.append({"orderNumber": n.get("orderNumber"), "date": _date(n.get("timestamp")),
                     "status": status_label(n.get("status")), "statusCode": n.get("status"),
                     "buyer": ((n.get("buyer") or {}).get("buyerSnapshot") or {}).get("institutionName"),
                     "product": (fi.get("snapshot") or {}).get("name"), "qty": fi.get("qty"),
                     "priceFirst": fi.get("originalPrice"), "priceLast": fi.get("lastPrice"),
                     "totalFirst": n.get("originalTotal"), "totalLast": n.get("latestTotal"),
                     "version": n.get("version"), "deadline": _date(n.get("expiryTime")) if n.get("expiryTime") else "-",
                     "items": n.get("rowItemCount"), "id": n.get("id"), "orderId": n.get("orderId"),
                     "orderKey": n.get("orderKey")})
    return rows


def _list_args(a):
    return a.per_page, a.all, a.page


def cmd_orders(a):
    from inaproc import emit, table
    b, c = _client(a)
    try:
        items, total, last = _paged(c, "OrderListMultishipment", "orderListV2", *_list_args(a))
    finally:
        b.close()
    rows = [r for r in order_rows(items) if _matches_status(r["statusCode"], a.status)]
    md = (f"**Pesanan Anda** — {len(rows)} ditampilkan dari {total} pesanan" +
          ("" if a.all else f" (halaman {a.page} dari {last}; semua: --all)") + "\n\n" +
          table(["No. pesanan", "Tanggal", "Status", "Instansi", "Produk (item pertama)", "Qty", "Item", "Total",
                 "Nego", "Kurir"],
                [[r["orderNumber"], r["date"], r["status"], r["buyer"], r["product"],
                  f"{r['qty']} {r['unit'] or ''}".strip(), r["items"], _money(r["total"]),
                  "ya" if r["viaNegotiation"] else "-", r["courier"]] for r in rows]) +
          "\n\nDetail satu pesanan:  penyedia order <No. pesanan>")
    emit(a, "penyedia-orders", a.status or "all", {"total": total, "items": rows}, md, {"orders": rows})


def cmd_order(a):
    from inaproc import emit, table, InaprocError
    b, c = _client(a)
    try:
        hit = _lookup(c, "OrderListMultishipment", a.ref)
        if not hit:
            raise InaprocError(f"pesanan {a.ref} tidak ditemukan di daftar pesanan Anda")
        o = c.gql("orderDetailV2", {"input": {"id": hit["id"], "orderKey": hit["orderKey"]}}) or {}
    finally:
        b.close()
    ppk = (((o.get("buyerInfo") or {}).get("assignedPPK") or {}).get("buyerSnapshot") or {})
    ship = o.get("shipmentSummary") or {}
    items = []
    for it in o.get("items") or []:
        snap = it.get("snapshot") or {}
        tax = it.get("tax") or {}
        items.append({"product": snap.get("name"), "qty": it.get("qty"),
                      "unit": (snap.get("stockUnit") or {}).get("primaryUnit"),
                      "priceFirst": it.get("originalPrice"), "priceLast": it.get("lastPrice"),
                      "subTotal": it.get("subTotal"), "ppnPercent": tax.get("ppnPercentage"), "ppn": tax.get("ppn")})
    summary = [["No. pesanan", o.get("orderNumber")], ["Status", f"{status_label(o.get('status'))} ({o.get('status')})"],
               ["Tanggal", _date(o.get("timestamp"))], ["Instansi", ppk.get("institutionName")],
               ["Satuan kerja", ppk.get("nameSatuanKerja")], ["PPK", ppk.get("namaUser")],
               ["Lewat negosiasi", "ya" if o.get("viaNegotiation") else "tidak"],
               ["Pengiriman", " ".join(x for x in (ship.get("provider"), ship.get("service")) if x) or "-"],
               ["Ongkir", _money(ship.get("totalShipmentPrice"))], ["Total sebelum pajak", _money(o.get("totalWithoutTax"))],
               ["Total", _money(o.get("total"))]]
    md = (f"**Pesanan {o.get('orderNumber')}**\n\n" + table(["", ""], summary) + "\n\n**Barang**\n\n" +
          table(["Produk", "Qty", "Harga awal", "Harga akhir", "Subtotal", "PPN"],
                [[i["product"], f"{i['qty']} {i['unit'] or ''}".strip(), _money(i["priceFirst"]),
                  _money(i["priceLast"]), _money(i["subTotal"]),
                  f"{i['ppnPercent']}% · {_money(i['ppn'])}" if i["ppnPercent"] is not None else "-"] for i in items]) +
          (f"\n\nNegosiasi:  penyedia negotiation {o.get('orderNumber')}" if o.get("viaNegotiation") else ""))
    emit(a, "penyedia-order", o.get("orderNumber") or a.ref, o, md,
         {"order": [dict(summary)], "items": items})


def cmd_negotiations(a):
    from inaproc import emit, table
    b, c = _client(a)
    try:
        items, total, last = _paged(c, "getNegotiationList", "negotiationListV2", *_list_args(a))
    finally:
        b.close()
    rows = [r for r in nego_rows(items) if _matches_status(r["statusCode"], a.status)]
    md = (f"**Negosiasi Anda** — {len(rows)} ditampilkan dari {total}" +
          ("" if a.all else f" (halaman {a.page} dari {last}; semua: --all)") + "\n\n" +
          table(["No. pesanan", "Tanggal", "Status", "Instansi", "Produk (item pertama)", "Qty", "Harga awal",
                 "Harga terakhir", "Total awal", "Total terakhir", "Versi", "Batas waktu"],
                [[r["orderNumber"], r["date"], r["status"], r["buyer"], r["product"], r["qty"], _money(r["priceFirst"]),
                  _money(r["priceLast"]), _money(r["totalFirst"]), _money(r["totalLast"]), r["version"], r["deadline"]]
                 for r in rows]) +
          "\n\nRiwayat tawar-menawar:  penyedia negotiation <No. pesanan>")
    emit(a, "penyedia-negotiations", a.status or "all", {"total": total, "items": rows}, md, {"negotiations": rows})


def cmd_negotiation(a):
    from inaproc import emit, table, InaprocError
    b, c = _client(a)
    try:
        hit = _lookup(c, "getNegotiationList", a.ref)
        if not hit:
            raise InaprocError(f"negosiasi untuk {a.ref} tidak ditemukan di daftar negosiasi Anda")
        n = c.gql("negotiationDetailV3", {"input": {"orderNegotiationId": hit["id"], "orderId": hit["orderId"],
                                                    "orderKey": hit["orderKey"]}}) or {}
    finally:
        b.close()
    reasons = n.get("reasons") or {}
    why = []
    for key, label in (("cancelReason", "Alasan batal"), ("rejectReason", "Alasan ditolak")):
        r = reasons.get(key)
        if r:
            why.append([label, " · ".join(x for x in (r.get("reason"), r.get("reasonDescription"), _date(r.get("timestamp")))
                                          if x and x != "-")])
    summary = [["No. pesanan", hit.get("orderNumber")], ["Status", f"{status_label(n.get('status'))} ({n.get('status')})"],
               ["Mulai", _date(n.get("timestamp"))],
               ["Instansi", ((hit.get("buyer") or {}).get("buyerSnapshot") or {}).get("institutionName")],
               ["Total awal", _money(n.get("originalTotal"))], ["Total terakhir", _money(n.get("latestTotal"))]] + why
    history, items = [], []
    for it in (n.get("negotiation") or {}).get("items") or []:
        d = it.get("itemDetail") or {}
        name = (d.get("snapshot") or {}).get("name")
        items.append({"product": name, "qty": d.get("qty"), "priceFirst": d.get("originalPrice"),
                      "priceLast": d.get("lastPrice"), "agreed": it.get("agreementPrice")})
        for h in it.get("history") or []:
            history.append({"product": name, "time": _date(h.get("createdAt")),
                            "by": ROLE_ID.get(h.get("proposerRole"), h.get("proposerRole")),
                            "old": h.get("oldPrice"), "new": h.get("newPrice")})
    md = (f"**Negosiasi {hit.get('orderNumber')}**\n\n" + table(["", ""], summary) + "\n\n**Barang**\n\n" +
          table(["Produk", "Qty", "Harga awal", "Harga terakhir", "Harga sepakat"],
                [[i["product"], i["qty"], _money(i["priceFirst"]), _money(i["priceLast"]),
                  _money(i["agreed"]) if i["agreed"] else "-"] for i in items]) +
          "\n\n**Riwayat tawaran**\n\n" +
          (table(["Waktu", "Diusulkan oleh", "Produk", "Harga lama", "Harga baru"],
                 [[h["time"], h["by"], h["product"], _money(h["old"]), _money(h["new"])] for h in history])
           if history else "_Belum ada tawaran._"))
    emit(a, "penyedia-negotiation", hit.get("orderNumber") or a.ref, n, md,
         {"negotiation": [dict(summary)], "items": items, "history": history})


# ----------------------------------------------------------------------------- CLI wiring
def add_parsers(sub, common):
    p = sub.add_parser("penyedia", help="your own seller account on penyedia.inaproc.id (your Safari, or Chrome)")
    ps = p.add_subparsers(dest="penyedia_cmd", required=True)

    def add(name, fn, help_):
        s = ps.add_parser(name, help=help_, parents=[common])
        s.set_defaults(fn=fn)
        s.add_argument("--browser", choices=["safari", "chrome"], default=default_browser(),
                       help="safari = your own Safari (default on macOS); chrome = the skill's Chrome profile")
        s.add_argument("--chrome-path", help="Chrome/Chromium/Edge/Brave binary (default: found automatically)")
        return s

    s = add("login", cmd_login, "open penyedia in your browser and wait while you sign in yourself")
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
    pp = ps.add_parser("product", help="one product: get, create, update, delete (writes are dry runs)")
    pps = pp.add_subparsers(dest="product_cmd", required=True)
    _parent = ps

    def padd(name, fn, help_):
        sp = pps.add_parser(name, help=help_, parents=[common])
        sp.set_defaults(fn=fn)
        sp.add_argument("--browser", choices=["safari", "chrome"], default=default_browser())
        sp.add_argument("--chrome-path")
        return sp

    s = padd("get", cmd_product_get, "show one of your products in full")
    s.add_argument("id")
    s = padd("create", cmd_create, "dry run: new product cloned from one of yours (or from a spec JSON)")
    s.add_argument("spec", nargs="?", help="CreateProductInput JSON (advanced)")
    s.add_argument("--from", dest="from_id", help="your product to copy (category, specs, images, shipping)")
    s.add_argument("--name", help="name of the new product (min 5 characters, must be unique)")
    s.add_argument("--price", help="price of the (single) variant")
    s.add_argument("--stock", help="stock of the (single) variant")
    s.add_argument("--no-images", action="store_true",
                   help="leave images out (add them in the site afterwards; image files cannot be shared)")
    s.add_argument("--submit", action="store_true",
                   help="create and submit for LKPP curation (default: save as a draft)")
    s = padd("update", cmd_update, "dry run: change price, stock, name or any field")
    s.add_argument("id")
    s.add_argument("--price")
    s.add_argument("--stock")
    s.add_argument("--variant", help="variant id or SKU (needed when the product has several)")
    s.add_argument("--name")
    s.add_argument("--set", action="append", help="dotted.path=value on the update input (repeatable)")
    s = padd("delete", cmd_delete, "dry run: delete one or more of your products")
    s.add_argument("ids", nargs="+")
    for name, fn, help_ in (("orders", cmd_orders, "pesanan Anda (status, instansi, total) — hanya baca"),
                            ("negotiations", cmd_negotiations, "negosiasi Anda (harga awal vs terakhir) — hanya baca")):
        s = add(name, fn, help_)
        s.add_argument("--status", help="saring status, mis. selesai, batal, kedaluwarsa, negosiasi")
        s.add_argument("--page", type=int, default=1)
        s.add_argument("--per-page", type=int, default=20)
        s.add_argument("--all", action="store_true", help="ambil semua halaman")
    s = add("order", cmd_order, "detail satu pesanan (barang, harga, PPK, pengiriman) — hanya baca")
    s.add_argument("ref", help="No. pesanan (EP-…), id, atau akhiran nomornya")
    s = add("negotiation", cmd_negotiation, "detail satu negosiasi + riwayat tawaran — hanya baca")
    s.add_argument("ref", help="No. pesanan (EP-…) atau id negosiasi")
    s = add("categories", cmd_categories, "categories your company's KBLI allows (as the add-product form checks)")
    s.add_argument("--search", help="only categories whose path contains this text")
    s.add_argument("--type", choices=["barang", "jasa", "digital"])
    s.add_argument("--all", action="store_true", help="also list the categories you are not eligible for")
    s.add_argument("--refresh", action="store_true", help="re-read the category KBLI list (cached 7 days)")
    s.add_argument("--seller-id", help="your institution/seller id (default: read from one of your products)")
    s = add("bulk-update", cmd_bulk, "dry run: update many products from a CSV/XLSX (columns id, price, stock, …)")
    s.add_argument("file")
    s = add("apply", cmd_apply, "send exactly the writes in a plan file (only after you approved it)")
    s.add_argument("plan")
    s.add_argument("--continue-on-error", action="store_true")
    s = add("record", cmd_record, "record the site's API calls while you work in the window")
    s.add_argument("--duration", type=float, help="stop after this many minutes (default: when the window closes)")
    return p
