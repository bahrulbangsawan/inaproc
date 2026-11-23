#!/usr/bin/env python3
"""inaproc — search Indonesia's government e-catalogue (katalog.inaproc.id).

Works with the Python 3.8+ standard library alone, sending Chrome-style request
headers. The `discover` command (and the `penyedia` seller commands) drive a
locally installed Chrome over the DevTools Protocol, with no extra packages.
The catalogue commands call only the public, no-login GraphQL operations.

Examples:
  python3 inaproc.py search "kertas hvs a4" --sort price-asc --type barang
  python3 inaproc.py detail https://katalog.inaproc.id/anak-terusan/kertas-hvs
  python3 inaproc.py cheapest "laptop core i5" --pages 2 --save xlsx,csv --out-dir .
  python3 inaproc.py compare <url1> <url2> --save json --out-dir ~/Documents
  python3 inaproc.py seller anak-terusan --products 10
  python3 inaproc.py read-xlsx RAB.xlsx
  python3 inaproc.py rab-report spec.json --verify --save xlsx --out-dir .
  python3 inaproc.py discover --wait 60          # needs a local Chrome/Chromium/Edge/Brave
"""
import argparse
import csv
import gzip
import http.client
import http.cookies
import platform
import datetime as _dt
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape as _xml_escape

# Run as a script, this file is __main__; register it as `inaproc` too so the helper modules beside it
# (inaproc_penyedia) share these classes instead of importing a second copy.
sys.modules.setdefault("inaproc", sys.modules[__name__])

ENDPOINT = "https://katalog.inaproc.id/graphql"
SITE = "https://katalog.inaproc.id"
VERSION = "1.5.2"
CHROME_MAJOR = 158  # keep User-Agent and sec-ch-ua on the same Chrome version
MIN_INTERVAL_FLOOR = 0.5   # never faster than 2 request starts per second, whatever the flags say
MAX_CONCURRENCY = 4
HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(HERE, "queries.json"), encoding="utf-8") as fh:
    QUERIES = json.load(fh)

SORTS = {
    "relevance": ("RELEVANCE", "DESC"), "price-asc": ("PRICE", "ASC"), "price-desc": ("PRICE", "DESC"),
    "sold": ("UNIT_SOLD", "DESC"), "tkdn": ("TKDN_BMP", "DESC"), "stock": ("STOCK_ACCUMULATION", "DESC"),
    "newest": ("UPDATED_AT", "DESC"),
}
TYPES = {"barang": "PHYSICAL", "physical": "PHYSICAL", "jasa": "SERVICE", "service": "SERVICE",
         "digital": "DIGITAL"}
SELLER_TYPES = {"umkk": "UMKK", "official": "OFFICIAL_VENDOR", "official_vendor": "OFFICIAL_VENDOR", "oap": "OAP"}
FORMATS = ("json", "csv", "xlsx", "yaml")


class InaprocError(Exception):
    pass


# ======================================================================= transport
class RateLimiter:
    """Spaces request starts at least `interval` seconds apart, across threads."""

    def __init__(self, interval):
        self.interval = max(MIN_INTERVAL_FLOOR, interval)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            time.sleep(start - now)



def chrome_platform():
    sysname = platform.system()
    return {"Darwin": "macOS", "Windows": "Windows"}.get(sysname, "Linux")


def user_agent():
    os_part = {"macOS": "Macintosh; Intel Mac OS X 10_15_7", "Windows": "Windows NT 10.0; Win64; x64",
               "Linux": "X11; Linux x86_64"}[chrome_platform()]
    return (f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36")


def chrome_headers(body_len, cookie, host="katalog.inaproc.id"):
    """Headers for a same-origin GraphQL fetch, in the order Chrome sends them over HTTP/1.1."""
    sec_ch_ua = (f'"Chromium";v="{CHROME_MAJOR}", "Google Chrome";v="{CHROME_MAJOR}", '
                 '"Not_A Brand";v="99"')
    h = [("Host", host), ("Connection", "keep-alive"), ("Content-Length", str(body_len)),
         ("sec-ch-ua-platform", f'"{chrome_platform()}"'), ("User-Agent", user_agent()),
         ("sec-ch-ua", sec_ch_ua), ("Content-Type", "application/json"), ("sec-ch-ua-mobile", "?0"),
         ("Accept", "application/json"), ("Origin", SITE), ("Sec-Fetch-Site", "same-origin"),
         ("Sec-Fetch-Mode", "cors"), ("Sec-Fetch-Dest", "empty"), ("Referer", SITE + "/"),
         ("Accept-Encoding", "gzip, deflate"), ("Accept-Language", "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7")]
    if cookie:
        h.append(("Cookie", cookie))
    return h


class CookieStore:
    """Cookies set by the one host this client talks to (name -> value, insertion ordered)."""

    def __init__(self):
        self._c = {}
        self._lock = threading.Lock()

    def update(self, set_cookie_headers):
        with self._lock:
            for raw in set_cookie_headers:
                jar = http.cookies.SimpleCookie()
                try:
                    jar.load(raw)
                except http.cookies.CookieError:
                    continue
                for name, morsel in jar.items():
                    self._c[name] = morsel.value

    def header(self):
        with self._lock:
            return "; ".join(f"{k}={v}" for k, v in self._c.items()) or None


def _decode_body(raw, encoding):
    encoding = (encoding or "").lower()
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", "replace")


def _progress(label):
    """Show `label` as the running step when the standalone app's live UI is active (no-op otherwise)."""
    ui = sys.modules.get("inaproc_ui")
    if ui is not None:
        ui.progress(label)


class Client:
    """One persistent session for the whole run (cookies + keep-alive), standard library only."""

    engine = "stdlib"

    def __init__(self, proxy=None, min_interval=1.0, concurrency=1, timeout=30, endpoint=ENDPOINT):
        self.limiter = RateLimiter(min_interval)
        self.concurrency = max(1, min(MAX_CONCURRENCY, concurrency))
        self.timeout = timeout
        self.proxy = proxy
        self._local = threading.local()
        self._conns = []
        self._lock = threading.Lock()
        self.cookies = CookieStore()
        u = urllib.parse.urlsplit(endpoint)
        self._scheme, self._host, self._port = u.scheme, u.hostname, u.port
        self._path = u.path or "/"
        self._host_header = "katalog.inaproc.id" if endpoint == ENDPOINT else u.netloc

    def _new_conn(self):
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        if self.proxy:
            p = urllib.parse.urlsplit(self.proxy)
            conn = cls(p.hostname, p.port or (443 if p.scheme == "https" else 80), timeout=self.timeout)
            headers = {}
            if p.username:
                import base64
                cred = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
                headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
            conn.set_tunnel(self._host, self._port or (443 if self._scheme == "https" else 80), headers=headers)
        else:
            conn = cls(self._host, self._port, timeout=self.timeout)
        with self._lock:
            self._conns.append(conn)
        return conn

    def _post(self, body):
        """POST one GraphQL body with Chrome-ordered headers on this thread's keep-alive connection."""
        for attempt in (0, 1):
            conn = getattr(self._local, "conn", None)
            if conn is None:
                conn = self._local.conn = self._new_conn()
            try:
                conn.putrequest("POST", self._path, skip_host=True, skip_accept_encoding=True)
                for k, v in chrome_headers(len(body), self.cookies.header(), host=self._host_header):
                    conn.putheader(k, v)
                conn.endheaders(body)
                resp = conn.getresponse()
                raw = resp.read()
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError,
                    http.client.CannotSendRequest, http.client.ResponseNotReady):
                conn.close()
                self._local.conn = None
                if attempt:
                    raise
                continue
            self.cookies.update(resp.headers.get_all("Set-Cookie") or [])
            if (resp.getheader("Connection") or "").lower() == "close":
                conn.close()
                self._local.conn = None
            return resp.status, _decode_body(raw, resp.getheader("Content-Encoding"))

    def gql(self, op, variables):
        _progress(f"Katalog · {op}")
        if op not in QUERIES:
            raise InaprocError(f"unknown operation {op!r}; run `ops` to list them")
        body = json.dumps({"operationName": op, "query": QUERIES[op], "variables": variables}).encode()
        post = self._post
        for attempt in range(3):
            self.limiter.wait()
            try:
                status, text = post(body)
            except Exception as e:
                if attempt < 2:
                    time.sleep(3)
                    continue
                raise InaprocError(f"network error reaching {ENDPOINT}: {e}. If this runs in a sandbox, "
                                   "allow network access to katalog.inaproc.id.")
            if status in (429, 502, 503, 504) and attempt < 2:
                time.sleep(5 * (attempt + 1) ** 2)  # 5s then 20s: the CDN rate-limits bursts
                continue
            if status == 401:
                raise InaprocError(f"{op}: HTTP 401, this operation needs a login (not supported)")
            if status == 400:
                raise InaprocError(f"{op}: bad input: {text[:300]}")
            if status in (429, 503):
                raise InaprocError(f"{op}: HTTP {status}, the site is rate-limiting; wait a minute and retry")
            if status != 200:
                raise InaprocError(f"{op}: HTTP {status}: {text[:200]}")
            break
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise InaprocError(f"{op}: the site answered with non-JSON (maybe a challenge page): {text[:150]}")
        if payload.get("errors") and not payload.get("data"):
            raise InaprocError(f"{op}: {payload['errors'][0].get('message')}")
        data = payload.get("data") or {}
        for value in data.values():
            if isinstance(value, dict) and value.get("__typename") in ("GenericError", "Error"):
                raise InaprocError(f"{op}: {value.get('code')}: {value.get('message')}")
        return data

    def gql_many(self, calls):
        """Run [(op, vars), ...] concurrently (bounded and still rate-limited); keeps order."""
        if self.concurrency == 1 or len(calls) < 2:
            return [self.gql(op, v) for op, v in calls]
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            return list(pool.map(lambda c: self.gql(*c), calls))

    def close(self):
        with self._lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass


CLIENT = None  # set in main()


def gql(op, variables):
    return CLIENT.gql(op, variables)


# ======================================================================= formatting helpers
def rp(n):
    if n is None:
        return "-"
    return "Rp" + f"{float(n):,.0f}".replace(",", ".")


def price_range(lo, hi):
    if lo is None and hi is None:
        return "-"
    if lo == hi or hi is None:
        return rp(lo)
    if lo is None:
        return rp(hi)
    return f"{rp(lo)} – {rp(hi)}"


def deepest(loc):
    name = None
    while isinstance(loc, dict):
        name = loc.get("name") or name
        loc = loc.get("child")
    return name or "-"


def parse_product_ref(ref):
    ref = re.sub(r"^https?://", "", ref.strip())
    ref = re.sub(r"^(www\.)?katalog\.inaproc\.id/", "", ref)
    parts = ref.split("?")[0].split("#")[0].strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise InaprocError(f"cannot read product reference {ref!r}; "
                           "use https://katalog.inaproc.id/<seller>/<product-slug>")
    return parts[0], parts[1]


def parse_seller_ref(ref):
    ref = re.sub(r"^https?://", "", ref.strip())
    ref = re.sub(r"^(www\.)?katalog\.inaproc\.id/", "", ref)
    return ref.split("?")[0].strip("/").split("/")[0]


def product_url(username, slug):
    return f"{SITE}/{username}/{slug}"


def cell(v, width=60):
    s = "-" if v is None or v == "" else str(v)
    s = s.replace("|", "/").replace("\n", " ")
    if s.startswith("http"):
        return s  # never cut a link
    return s if len(s) <= width else s[: width - 1] + "…"


def table(headers, rows, width=60):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(cell(c, width) for c in r) + " |" for r in rows]
    return "\n".join(out)


# ======================================================================= exports
def _scalar(v):
    if isinstance(v, list):
        if all(not isinstance(x, (dict, list)) for x in v):
            return "; ".join("" if x is None else str(x) for x in v)
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False) if v else ""
    return v


def _flatten(obj, prefix="", out=None):
    """Nested dict -> one flat row with dotted column names (for CSV/XLSX)."""
    out = {} if out is None else out
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict) and v:
                _flatten(v, key, out)
            else:
                out[key] = _scalar(v)
    else:
        out[prefix or "value"] = _scalar(obj)
    return out


def flat_rows(rows):
    return [_flatten(r) for r in (rows or [])]


def to_yaml(o, indent=0):
    """Small YAML emitter (block style; strings are JSON-quoted, which YAML accepts)."""
    pad = "  " * indent
    if isinstance(o, dict):
        if not o:
            return "{}"
        lines = []
        for k, v in o.items():
            key = str(k) if re.match(r"^[A-Za-z_][\w.-]*$", str(k)) else json.dumps(str(k), ensure_ascii=False)
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{key}:\n{to_yaml(v, indent + 1)}")
            else:
                lines.append(f"{pad}{key}: {to_yaml(v)}")
        return "\n".join(lines)
    if isinstance(o, list):
        if not o:
            return "[]"
        lines = []
        for v in o:
            if isinstance(v, dict) and v:
                inner = to_yaml(v, indent + 1).split("\n")
                lines.append(f"{pad}- {inner[0].lstrip()}")
                lines.extend(inner[1:])
            elif isinstance(v, list) and v:
                lines.append(f"{pad}-\n{to_yaml(v, indent + 1)}")
            else:
                lines.append(f"{pad}- {to_yaml(v)}")
        return "\n".join(lines)
    if o is None:
        return "null"
    if isinstance(o, bool):
        return "true" if o else "false"
    if isinstance(o, (int, float)):
        return repr(o)
    return json.dumps(str(o), ensure_ascii=False)


_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _col(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _sheet_xml(rows):
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    widths = [min(60, max([len(str(c))] + [len(str(r.get(c, "") or "")) for r in rows[:200]]) + 2) for c in cols]

    def c(ref, v, style=""):
        if isinstance(v, bool):
            return f'<c r="{ref}" t="b"{style}><v>{int(v)}</v></c>'
        if isinstance(v, (int, float)) and v == v:
            return f'<c r="{ref}"{style}><v>{v}</v></c>'
        if v is None or v == "":
            return ""
        t = _xml_escape(_ILLEGAL_XML.sub("", str(v))[:32767])
        return f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">{t}</t></is></c>'

    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
           '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
           'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>']
    if cols:
        out.append("<cols>" + "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
                                      for i, w in enumerate(widths, 1)) + "</cols>")
    out.append("<sheetData>")
    out.append('<row r="1">' + "".join(c(f"{_col(i)}1", h, ' s="1"') for i, h in enumerate(cols, 1)) + "</row>")
    for ri, r in enumerate(rows, 2):
        out.append(f'<row r="{ri}">' + "".join(c(f"{_col(i)}{ri}", r.get(h)) for i, h in enumerate(cols, 1)) + "</row>")
    out.append("</sheetData></worksheet>")
    return "".join(out)


def write_xlsx(path, sheets):
    """Standard-library .xlsx writer: one worksheet per sheet, bold frozen header row."""
    names, used = [], set()
    for name in sheets:
        n = re.sub(r"[\[\]:*?/\\]", "_", name)[:31] or "Sheet"
        base, i = n, 2
        while n.lower() in used:
            n = f"{base[:28]}_{i}"
            i += 1
        used.add(n.lower())
        names.append(n)
    ns_main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ns_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ns_pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", xml +
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(1, len(names) + 1)) + "</Types>")
        z.writestr("_rels/.rels", xml + f'<Relationships xmlns="{ns_pkg}">'
                   f'<Relationship Id="rId1" Type="{ns_rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", xml + f'<workbook xmlns="{ns_main}" xmlns:r="{ns_rel}"><sheets>'
                   + "".join(f'<sheet name="{_xml_escape(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(names, 1))
                   + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", xml + f'<Relationships xmlns="{ns_pkg}">'
                   + "".join(f'<Relationship Id="rId{i}" Type="{ns_rel}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                             for i in range(1, len(names) + 1))
                   + f'<Relationship Id="rId{len(names) + 1}" Type="{ns_rel}/styles" Target="styles.xml"/></Relationships>')
        z.writestr("xl/styles.xml", xml + f'<styleSheet xmlns="{ns_main}">'
                   '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
                   '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
                   '<fills count="2"><fill><patternFill patternType="none"/></fill>'
                   '<fill><patternFill patternType="gray125"/></fill></fills>'
                   '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                   '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                   '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                   '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
                   '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
                   "</styleSheet>")
        for i, rows in enumerate(sheets.values(), 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows))


def slugify(text, limit=40):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit].strip("-") or "result"


def save_outputs(formats, out_dir, cmd, label, full, sheets):
    """Write <out_dir>/output/<cmd>_<label>_<timestamp>.<ext> for each requested format."""
    target = os.path.join(os.path.abspath(os.path.expanduser(out_dir)), "output")
    os.makedirs(target, exist_ok=True)
    now = _dt.datetime.now().astimezone()
    stem = f"{cmd}_{slugify(label)}_{now.strftime('%Y%m%d-%H%M%S')}"
    meta = {"source": SITE, "command": cmd, "query": label, "generatedAt": now.isoformat(timespec="seconds")}
    sheets = {k: flat_rows(v) for k, v in sheets.items() if v is not None}
    written = []
    for fmt in formats:
        if fmt == "json":
            p = os.path.join(target, stem + ".json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"meta": meta, "data": full}, f, ensure_ascii=False, indent=2)
            written.append(p)
        elif fmt == "yaml":
            p = os.path.join(target, stem + ".yaml")
            with open(p, "w", encoding="utf-8") as f:
                f.write(to_yaml({"meta": meta, "data": full}) + "\n")
            written.append(p)
        elif fmt == "xlsx":
            p = os.path.join(target, stem + ".xlsx")
            write_xlsx(p, {k: v for k, v in sheets.items() if v} or {"data": []})
            written.append(p)
        elif fmt == "csv":
            many = len(sheets) > 1
            for name, rows in sheets.items():
                if many and not rows:
                    continue
                p = os.path.join(target, f"{stem}__{name}.csv" if many else stem + ".csv")
                cols = []
                for r in rows:
                    for k in r:
                        if k not in cols:
                            cols.append(k)
                with open(p, "w", encoding="utf-8-sig", newline="") as f:  # BOM so Excel opens UTF-8 correctly
                    w = csv.DictWriter(f, fieldnames=cols or ["empty"])
                    w.writeheader()
                    w.writerows(rows)
                written.append(p)
    return written


def emit(args, cmd, label, full, md, sheets):
    if args.json:
        print(json.dumps(full, ensure_ascii=False, indent=2))
    else:
        print(md)
    if args.save:
        written = save_outputs(args.save, args.out_dir, cmd, label, full, sheets)
        print("\nSaved:\n" + "\n".join(f"- {p}" for p in written))


# ======================================================================= search
def search_input(keyword, page=1, per_page=30, sort="relevance", types=None, labels=None,
                 seller_types=None, min_price=None, max_price=None, category_ids=None, seller_id=None):
    field, order = SORTS[sort]
    flt = {"strategy": "SRP", "keyword": keyword or None, "labels": labels or [],
           "sellerTypes": seller_types or [], "sellerRegionCodes": [""], "minPrice": min_price,
           "maxPrice": max_price, "rateTypes": [], "productTypes": types or [], "ratingAvgGte": None}
    if category_ids:
        flt["categoryIds"] = category_ids
    if seller_id:
        flt["sellerId"] = seller_id  # the seller's institusiId (= a product's sellerId)
    return {"sort": [{"field": field, "order": order}], "filter": flt,
            "pagination": {"page": page, "perPage": per_page}}


def slim_item(i):
    tk = i.get("tkdn") or {}
    rating = i.get("rating") or {}
    return {
        "name": i.get("name"), "url": product_url(i.get("username"), i.get("slug")), "id": i.get("id"),
        "priceMinWithTax": i.get("minPriceWithTax"), "priceMaxWithTax": i.get("maxPriceWithTax"),
        "priceMinBeforeTax": i.get("minPrice"), "ppnPercent": i.get("ppnPercentage"),
        "seller": i.get("sellerName"), "sellerUsername": i.get("username"), "sellerId": i.get("sellerId"),
        "sellerUMKK": i.get("isSellerUMKK"), "location": deepest(i.get("location")),
        "labels": i.get("labels") or [], "tkdnBmp": tk.get("tkdnBmp"), "unitSold": i.get("unitSold"),
        "stock": i.get("stockAccumulation"), "rating": rating.get("average"), "ratingCount": rating.get("count"),
        "category": (i.get("category") or {}).get("name"), "brand": (i.get("brand") or {}).get("brandName"),
        "type": i.get("type"), "variantCount": len(i.get("variants") or []), "preOrder": i.get("isPreOrder"),
    }


def _search_result(data):
    data = data["searchProducts"]
    return {"total": data.get("total"), "page": data.get("currentPage"), "lastPage": data.get("lastPage"),
            "perPage": data.get("perPage"), "items": [slim_item(i) for i in (data.get("items") or [])]}


# ----------------------------------------------------------------------- category path (Level 1 › 2 › 3)
CATEGORY_ENRICH_LIMIT = 12


def _cache_path():
    home = os.environ.get("INAPROC_HOME") or os.path.join(os.path.expanduser("~"), ".inaproc")
    os.makedirs(os.path.join(home, "cache"), exist_ok=True)
    return os.path.join(home, "cache", "category_paths.json")


_CAT_CACHE = None


def _cat_cache():
    global _CAT_CACHE
    path = _cache_path()
    if _CAT_CACHE is None or _CAT_CACHE[0] != path:
        try:
            with open(path, encoding="utf-8") as f:
                _CAT_CACHE = (path, json.load(f))
        except (OSError, ValueError):
            _CAT_CACHE = (path, {})
    return _CAT_CACHE[1]


def _cat_save():
    try:
        with open(_cache_path(), "w", encoding="utf-8") as f:
            json.dump(_cat_cache(), f, ensure_ascii=False)
    except OSError:
        pass


def category_chain(cat_id):
    """[Level 1, Level 2, Level 3] names for a category id (cached on disk)."""
    if not cat_id:
        return []
    cache = _cat_cache()
    if cat_id not in cache:
        d = gql("getProductCategory", {"id": cat_id}).get("getProductCategory") or {}
        chain, cur = [], d
        while cur:
            chain.append(cur.get("name"))
            cur = cur.get("parent")
        cache[cat_id] = list(reversed(chain))
        _cat_save()
    return cache[cat_id]


def add_category_levels(item, chain):
    chain = (chain or []) + [None, None, None]
    item["categoryL1"] = chain[0] or item.get("category")
    item["categoryL2"], item["categoryL3"] = chain[1], " › ".join(x for x in chain[2:] if x) or None
    return item


def enrich_categories(items, limit=CATEGORY_ENRICH_LIMIT):
    """Search results only carry Level 1; look up each product's own category for Levels 2-3 (first `limit`)."""
    cache = _cat_cache()
    todo = [i for i in items[:limit] if f"p:{i['url']}" not in cache]
    if todo:
        details = CLIENT.gql_many([("getProductBySlug", {"username": i["sellerUsername"],
                                                         "slug": i["url"].rstrip("/").rsplit("/", 1)[-1],
                                                         "regionCode": None}) for i in todo])
        for i, d in zip(todo, details):
            cache[f"p:{i['url']}"] = ((d or {}).get("getProductBySlug") or {}).get("category", {}).get("id")
        _cat_save()
    for n, i in enumerate(items):
        add_category_levels(i, category_chain(cache.get(f"p:{i['url']}")) if n < limit else [i.get("category")])
    return items


def items_table(items, start=1):
    rows = [[n, i["name"], price_range(i["priceMinWithTax"], i["priceMaxWithTax"]), i["seller"], i["location"],
             f'{i["tkdnBmp"]}%' if i["tkdnBmp"] else "-", ",".join(i["labels"]) or "-", i["unitSold"] or 0,
             i.get("categoryL1") or i.get("category"), i.get("categoryL2"), i.get("categoryL3"), i["url"]]
            for n, i in enumerate(items, start)]
    return table(["#", "Product", "Price (incl. PPN)", "Seller", "Location", "TKDN+BMP", "Labels", "Sold",
                  "Kategori L1", "Kategori L2", "Kategori L3", "URL"], rows)


def search_kwargs(a):
    return dict(
        types=[TYPES[t.lower()] for t in a.type] if a.type else None,
        labels=[l.upper() for l in a.label] if a.label else None,
        seller_types=[SELLER_TYPES.get(s.lower(), s.upper()) for s in a.seller_type] if a.seller_type else None,
        min_price=a.min_price, max_price=a.max_price, category_ids=a.category or None, seller_id=a.seller_id)


def cmd_search(a):
    kw = search_kwargs(a)
    pages = list(range(a.page, a.page + max(1, a.pages)))
    res = [_search_result(r) for r in CLIENT.gql_many(
        [("searchProducts", {"input": search_input(a.keyword, p, a.per_page, a.sort, **kw)}) for p in pages])]
    items = [i for r in res for i in r["items"]]
    if not a.no_categories:
        enrich_categories(items)
    first = res[0]
    label = a.keyword or (f"seller-{a.seller_id}" if a.seller_id else "all")
    md = (f"**{first['total']:,} results** for “{a.keyword or '(any)'}” (page {pages[0]}"
          f"{'–' + str(pages[-1]) if len(pages) > 1 else ''} of {first['lastPage']}, sort: {a.sort}). "
          f"Prices include PPN.\n\n" + items_table(items, (a.page - 1) * a.per_page + 1))
    if not a.no_categories and len(items) > CATEGORY_ENRICH_LIMIT:
        md += (f"\n\n_Kategori L2/L3 diisi untuk {CATEGORY_ENRICH_LIMIT} baris pertama (L1 untuk sisanya); "
               f"pakai --per-page {CATEGORY_ENRICH_LIMIT} untuk detail kategori semua baris._")
    if first["total"] and first["total"] >= 10000:
        md += "\n\n_The site caps totals at 10,000: narrow with --type, --category, --min-price or --max-price._"
    full = {"total": first["total"], "lastPage": first["lastPage"], "pages": pages, "sort": a.sort, "items": items}
    emit(a, "search", label, full, md, {"products": items})


def cmd_cheapest(a):
    """Most relevant N pages, then rank by price; set aside suspiciously cheap outliers.

    The site's own price-ascending sort surfaces Rp1 placeholders, per-sheet prices and
    unrelated items first, so relevance-first then price-ranking finds the cheapest *match*.
    """
    kw = search_kwargs(a)
    results = CLIENT.gql_many([("searchProducts", {"input": search_input(a.keyword, p, 60, "relevance", **kw)})
                               for p in range(1, a.pages + 1)])
    pool, seen = [], set()
    for r in results:
        for i in _search_result(r)["items"]:
            if i["id"] not in seen and i["priceMinWithTax"] is not None:
                seen.add(i["id"])
                pool.append(i)
    words = [w for w in re.split(r"\s+", a.keyword.lower()) if w]
    if a.strict:
        pool = [i for i in pool if all(w in (i["name"] or "").lower() for w in words)]
    pool.sort(key=lambda i: i["priceMinWithTax"])
    outliers, median = [], None
    if pool and not a.include_outliers:
        median = pool[len(pool) // 2]["priceMinWithTax"]
        floor = median * a.outlier_ratio
        outliers = [i for i in pool if i["priceMinWithTax"] < floor]
        pool = [i for i in pool if i["priceMinWithTax"] >= floor]
    top = pool[: a.top]
    if not a.no_categories:
        enrich_categories(top)
    md = (f"**Cheapest {len(top)} of {len(pool)} relevant results** for “{a.keyword}” (searched {a.pages} page(s) "
          f"of 60 by relevance{', name must contain every keyword' if a.strict else ''}). Prices include PPN; check "
          "the unit and variant on the detail page before deciding.\n\n" + items_table(top))
    if outliers:
        md += (f"\n\n_{len(outliers)} listing(s) set aside as suspiciously cheap (below {int(a.outlier_ratio * 100)}% "
               f"of the median {rp(median)}), e.g. "
               + "; ".join(f"{o['name']} {rp(o['priceMinWithTax'])}" for o in outliers[:3])
               + ". Use --include-outliers to keep them._")
    full = {"keyword": a.keyword, "considered": len(pool), "medianPrice": median, "items": top, "setAside": outliers}
    emit(a, "cheapest", a.keyword, full, md, {"cheapest": top, "set_aside": outliers})


# ======================================================================= product detail
def fetch_details(refs, region=None):
    calls = []
    for r in refs:
        u, s = parse_product_ref(r)
        calls.append(("getProductBySlug", {"username": u, "slug": s, "regionCode": region}))
    out = []
    for ref, d in zip(refs, CLIENT.gql_many(calls)):
        p = d.get("getProductBySlug")
        if not p:
            raise InaprocError(f"product not found: {ref}")
        out.append(p)
    return out


def slim_detail(p):
    prices = p.get("prices") or {}
    tk = p.get("tkdn") or {}
    pdn = p.get("pdn") or {}
    info = p.get("productInformations") or {}
    specs = [{"name": x.get("name"), "value": x.get("value")}
             for x in (info.get("mainInformations") or []) + (info.get("additionalInformations") or [])
             if x.get("value")]
    variants = [{"id": v.get("id"), "option": " / ".join(v.get("optionValues") or []) or "default",
                 "price": v.get("price"), "priceWithTax": v.get("priceWithTax"), "stock": v.get("stock"),
                 "active": v.get("isActive"), "sku": v.get("sku")} for v in (p.get("variants") or [])]
    brand = p.get("brand")
    return {
        "name": p.get("name"), "url": product_url(p.get("username"), p.get("slug")), "id": p.get("id"),
        "status": p.get("status"), "type": p.get("type"), "category": (p.get("category") or {}).get("name"),
        "categoryId": (p.get("category") or {}).get("id"),
        "brand": brand.get("brandName") if isinstance(brand, dict) else None,
        "priceMinWithTax": prices.get("minPriceWithTax"), "priceMaxWithTax": prices.get("maxPriceWithTax"),
        "minPurchase": prices.get("minPurchase"), "wholesale": prices.get("productWholesalePrices") or [],
        "unit": (p.get("stockUnit") or {}).get("primaryUnit"), "variants": variants,
        "labels": p.get("labels") or [],
        "tkdn": {"value": tk.get("value"), "bmp": tk.get("bmpValue"), "status": tk.get("status"),
                 "certificate": tk.get("number"), "expires": (tk.get("expirationDate") or "")[:10] or None,
                 "producer": tk.get("producerName"), "url": tk.get("url")} if tk else None,
        "pdn": {"type": pdn.get("type"), "country": pdn.get("countryName")} if pdn else None,
        "sni": p.get("sni"), "ppnPercent": (p.get("tax") or {}).get("ppnPercentage"),
        "shipping": p.get("shipping"), "sellerUsername": p.get("username"), "sellerId": p.get("sellerId"),
        "sellerCity": (p.get("sellerLocation") or {}).get("cityName"), "unitSold": p.get("unitSold"),
        "rating": p.get("rating"), "description": (p.get("description") or "").strip(), "specs": specs,
        "images": [i.get("imageUrl") for i in (p.get("images") or []) if i.get("imageUrl")],
        "kbki": p.get("kbki"), "updatedAt": p.get("updatedAt"),
    }


def _product_row(d):
    row = {k: v for k, v in d.items() if k not in ("variants", "specs", "images", "wholesale")}
    row["variantCount"] = len(d["variants"])
    row["stockTotal"] = sum((v["stock"] or 0) for v in d["variants"]) if d["variants"] else None
    return row


def _detail_sheets(ds):
    return {
        "products": [_product_row(d) for d in ds],
        "variants": [dict({"product": d["name"], "url": d["url"]}, **v) for d in ds for v in d["variants"]],
        "specifications": [dict({"product": d["name"]}, **s) for d in ds for s in d["specs"]],
        "wholesale": [dict({"product": d["name"]}, **w) for d in ds for w in d["wholesale"]],
        "images": [{"product": d["name"], "imageUrl": u} for d in ds for u in d["images"]],
    }


def _with_levels(d):
    return add_category_levels(d, category_chain(d.get("categoryId")))


def cmd_detail(a):
    d = _with_levels(slim_detail(fetch_details([a.product], a.region)[0]))
    tk = d["tkdn"] or {}
    rating = d["rating"] or {}
    lines = [f"## {d['name']}", d["url"], "", table(["Field", "Value"], [
        ["Price (incl. PPN)", price_range(d["priceMinWithTax"], d["priceMaxWithTax"])],
        ["Unit / min. purchase", f"{d['unit'] or '-'} / {d['minPurchase'] or '-'}"],
        ["Kategori L1", d.get("categoryL1")], ["Kategori L2", d.get("categoryL2")],
        ["Kategori L3", d.get("categoryL3")], ["Brand", d["brand"]], ["Status", d["status"]],
        ["Labels", ", ".join(d["labels"]) or "-"],
        ["TKDN / BMP", f"{tk.get('value')}% / {tk.get('bmp')}" if tk.get("value") is not None else "-"],
        ["TKDN certificate", f"{tk.get('certificate') or '-'} (expires {tk.get('expires') or '-'})"],
        ["PDN origin", (d["pdn"] or {}).get("country")],
        ["PPN", f"{d['ppnPercent']}%" if d["ppnPercent"] is not None else "-"],
        ["Seller", f"{d['sellerUsername']} ({d['sellerCity'] or '-'})"], ["Sold", d["unitSold"]],
        ["Rating", f"{rating.get('average')} ({rating.get('count')} reviews)" if rating else "none yet"],
        ["Weight", f"{(d['shipping'] or {}).get('weight')} {(d['shipping'] or {}).get('weightUnit') or ''}"],
    ])]
    if d["variants"]:
        lines += ["", "**Variants**", "", table(["Option", "Price", "Price incl. PPN", "Stock"],
                  [[v["option"], rp(v["price"]), rp(v["priceWithTax"]), v["stock"]] for v in d["variants"]])]
    if d["wholesale"]:
        lines += ["", "**Wholesale prices**", "", table(["Min qty", "Price incl. PPN"],
                  [[w.get("minQuantity"), rp(w.get("priceWithTax"))] for w in d["wholesale"]])]
    if d["specs"]:
        lines += ["", "**Specifications**", "", table(["Spec", "Value"], [[s["name"], s["value"]] for s in d["specs"]])]
    if d["description"]:
        lines += ["", "**Description**", "", d["description"][:1500]]
    emit(a, "detail", f"{d['sellerUsername']}-{d['name']}", d, "\n".join(lines), _detail_sheets([d]))


def cmd_compare(a):
    if len(a.products) < 2:
        raise InaprocError("compare needs at least two products")
    ds = [_with_levels(slim_detail(p)) for p in fetch_details(a.products, a.region)]
    cheapest = min((d["priceMinWithTax"] for d in ds if d["priceMinWithTax"] is not None), default=None)

    def row(label, fn):
        return [label] + [fn(d) for d in ds]

    rows = [
        row("Product", lambda d: d["name"]),
        row("Price (incl. PPN)", lambda d: price_range(d["priceMinWithTax"], d["priceMaxWithTax"])
            + (" ✅ lowest" if d["priceMinWithTax"] == cheapest else "")),
        row("Unit", lambda d: d["unit"]), row("Min. purchase", lambda d: d["minPurchase"]),
        row("Variants", lambda d: len(d["variants"])),
        row("Stock (sum)", lambda d: sum((v["stock"] or 0) for v in d["variants"]) if d["variants"] else "-"),
        row("Brand", lambda d: d["brand"]), row("Kategori L1", lambda d: d.get("categoryL1")),
        row("Kategori L2", lambda d: d.get("categoryL2")), row("Kategori L3", lambda d: d.get("categoryL3")),
        row("TKDN %", lambda d: (d["tkdn"] or {}).get("value")), row("BMP", lambda d: (d["tkdn"] or {}).get("bmp")),
        row("Labels", lambda d: ", ".join(d["labels"])), row("PPN %", lambda d: d["ppnPercent"]),
        row("Seller", lambda d: d["sellerUsername"]), row("Seller city", lambda d: d["sellerCity"]),
        row("Sold", lambda d: d["unitSold"]),
        row("Rating", lambda d: (d["rating"] or {}).get("average") if d["rating"] else "none"),
        row("Weight", lambda d: f"{(d['shipping'] or {}).get('weight')} {(d['shipping'] or {}).get('weightUnit') or ''}"),
        row("URL", lambda d: d["url"]),
    ]
    md = "**Side-by-side comparison** (prices include PPN)\n\n" + table(
        ["Field"] + [f"#{n}" for n in range(1, len(ds) + 1)], rows)
    emit(a, "compare", "-vs-".join(d["sellerUsername"] for d in ds), {"products": ds}, md, _detail_sheets(ds))


def cmd_reviews(a):
    d = fetch_details([a.product])[0]
    pid = d["id"]
    agg_d, lst_d = CLIENT.gql_many([
        ("getOrderRatingAggregation", {"input": {"filter": {"productId": pid}, "aggregationType": "RATING"}}),
        ("getOrderProductRatingList", {"input": {"filter": {"productId": pid},
                                                 "pagination": {"page": a.page, "perPage": a.per_page}}}),
    ])
    agg, lst = agg_d["orderRatingAggregation"], lst_d["orderRatingList"]
    items = lst.get("items") or []
    md = (f"**{d['name']}**: average {agg.get('ratingAvg')} from {agg.get('ratingCount')} ratings, "
          f"{agg.get('reviewCount')} reviews.\n\n")
    md += table(["Date", "Rating", "Buyer (KLPD / Satker)", "Review"],
                [[(r.get("createdAt") or "")[:10], r.get("rating"),
                  f"{r.get('institutionKlpdName') or '-'} / {r.get('institutionSatkerName') or '-'}", r.get("review")]
                 for r in items]) if items else "_No reviews yet._"
    full = {"product": {"name": d["name"], "url": product_url(d["username"], d["slug"]), "id": pid},
            "summary": agg, "reviews": lst}
    emit(a, "reviews", d["name"], full, md, {"summary": [dict({"product": d["name"]}, **agg)], "reviews": items})


# ======================================================================= sellers
def cmd_seller(a):
    slug = parse_seller_ref(a.seller)
    comps = gql("getSellerInfoBySlug", {"filter": {"slugs": [slug]}})["companyPublic"].get("companies") or []
    if not comps:
        raise InaprocError(f"seller not found: {slug} (use the store slug from the URL, e.g. anak-terusan)")
    c = comps[0]
    sid = c.get("institusiId")
    calls = [("getSellerScore", {"input": {"sellerId": sid}}), ("getAssessmentSummaryDashboard", {"sellerId": sid})]
    if a.products:
        calls.append(("searchProducts", {"input": search_input(None, 1, a.products, "sold", seller_id=sid)}))
    res = CLIENT.gql_many(calls)
    score, assess = res[0]["sellerScore"], res[1]["scoringGetAssessmentSummaryDashboard"]
    prods = _search_result(res[2]) if a.products else None
    addr = (c.get("companyAddresses") or [{}])[0]
    ships = sorted({f"{(s.get('regionDetail') or {}).get('cityName')}, {(s.get('regionDetail') or {}).get('provinceName')}"
                    for s in (c.get("shippingAddresses") or [])})
    lines = [f"## {c.get('name')}", f"{SITE}/{slug}", "", table(["Field", "Value"], [
        ["Business form", c.get("bentukUsaha")], ["UMKK", "yes" if c.get("isUmkk") else "no"],
        ["Status", c.get("status")], ["Website", c.get("website")], ["Address", addr.get("fullAddress")],
        ["Province", (addr.get("regionDetail") or {}).get("provinceName")], ["Ships from", "; ".join(ships) or "-"],
        ["Seller score", score.get("finalScore") if score.get("finalScore") is not None else "not scored yet"],
        ["Signed orders", score.get("orderSignedCount")],
        ["Assessments", f"{assess.get('totalAssessment')} (avg {assess.get('avgScore')})"], ["Seller ID", sid],
    ])]
    if score.get("details"):
        lines += ["", "**Score breakdown**", "", table(["Component", "Value", "Weight", "Notes"], [
            [d.get("componentName"), f"{d.get('value')}{d.get('valueUnit') or ''}" if d.get("value") is not None else "-",
             f"{d.get('weightPercentage')}%" if d.get("weightPercentage") is not None else "-", d.get("notes")]
            for d in score["details"]])]
    if prods is not None:
        lines += ["", f"**Products: {prods['total']} listed**, top {len(prods['items'])} by units sold "
                  f"(more: `search --seller-id {sid}`)", "",
                  items_table(prods["items"]) if prods["items"] else "_no products listed_"]
    seller_row = dict(c, url=f"{SITE}/{slug}", finalScore=score.get("finalScore"),
                      orderSignedCount=score.get("orderSignedCount"),
                      totalAssessment=assess.get("totalAssessment"), avgAssessment=assess.get("avgScore"))
    emit(a, "seller", slug, {"company": c, "score": score, "assessment": assess, "products": prods},
         "\n".join(lines), {"seller": [seller_row], "score_breakdown": score.get("details") or [],
                            "products": (prods or {}).get("items") or None})


def cmd_sellers(a):
    inp = {"filter": {"keyword": a.keyword, "strategy": "DEFAULT", "labels": [], "sellerRegionCodes": [""]},
           "pagination": {"page": a.page, "perPage": a.per_page}}
    d = gql("getSearchSeller", {"input": inp})["searchSeller"]
    items = [{"name": i.get("name"), "slug": i.get("slug"), "url": f"{SITE}/{i.get('slug')}",
              "isUMKK": i.get("isUMKK"), "transactionCount": i.get("transactionCount"),
              "location": deepest(i.get("location")), "imageUrl": i.get("imageUrl")} for i in d.get("items") or []]
    md = f"**{d.get('total')} sellers** for “{a.keyword}”\n\n" + table(
        ["Seller", "UMKK", "Transactions", "Location", "Store URL"],
        [[i["name"], "yes" if i["isUMKK"] else "no", i["transactionCount"], i["location"], i["url"]] for i in items])
    emit(a, "sellers", a.keyword, {"total": d.get("total"), "items": items}, md, {"sellers": items})


# ======================================================================= reference data
def cmd_categories(a):
    d = gql("getAllMinifiedCategory", {"input": {"level": 1, "productType": TYPES[a.type.lower()]}})
    d = d["allMinifiedProductCategory"]
    lines, rows = [], []
    for top in d.get("items") or []:
        lines.append(f"- **{top['name']}** `{top['id']}`")
        rows.append({"level": 1, "id": top["id"], "name": top["name"], "path": top["name"]})
        for c2 in (top.get("children") or []) if a.depth >= 2 else []:
            lines.append(f"  - {c2['name']} `{c2['id']}`")
            rows.append({"level": 2, "id": c2["id"], "name": c2["name"], "path": f"{top['name']} › {c2['name']}"})
            for c3 in (c2.get("children") or []) if a.depth >= 3 else []:
                lines.append(f"    - {c3['name']} `{c3['id']}`")
                rows.append({"level": 3, "id": c3["id"], "name": c3["name"],
                             "path": f"{top['name']} › {c2['name']} › {c3['name']}"})
    emit(a, "categories", a.type, d, f"**Categories ({a.type})**, use an id with `search --category <id>`\n\n"
         + "\n".join(lines), {"categories": rows})


def cmd_category(a):
    d = gql("getProductCategory", {"id": a.id})["getProductCategory"]
    chain, cur = [], d
    while cur:
        chain.append(cur.get("name"))
        cur = cur.get("parent")
    path = " › ".join(reversed(chain))
    emit(a, "category", d.get("name"), d, f"**{d.get('name')}** (level {d.get('level')}): {path}",
         {"category": [{"id": d.get("id"), "name": d.get("name"), "level": d.get("level"), "path": path}]})


def _region_out(a, cmd, label, items):
    emit(a, cmd, label, items,
         table(["Name", "Area code", "Level"], [[i["name"], i["areaCode"], i["level"]] for i in items]),
         {"regions": items})


def cmd_regions(a):
    if a.province:
        d = gql("getAllRegionCity", {"regionLevel": "CITY", "parentAreaCodes": [a.province]})
    else:
        d = gql("getAllRegionProvince", {"regionLevel": "PROVINCE"})
    _region_out(a, "regions", a.province or "provinces", d["getAllRegionV2"].get("items") or [])


def cmd_region_search(a):
    d = gql("GetSearchRegion", {"level": a.level.upper(), "search": a.name,
                                "pagination": {"page": 1, "perPage": a.per_page}})["getSearchRegionV2"]
    _region_out(a, "region-search", a.name, d.get("items") or [])


def cmd_kbli(a):
    d = gql("getKBLIList", {"page": a.page, "perPage": a.per_page})["getAllKBLI"]
    items = d.get("items") or []
    emit(a, "kbli", f"page-{a.page}", d, table(["Code", "Title"], [[i["code"], i["title"]] for i in items]),
         {"kbli": items})


def cmd_ppn(a):
    d = gql("ppnList", {})["listPpn"]
    items = d.get("items") or []
    emit(a, "ppn", "rates", d, "PPN rates used on the catalogue: "
         + ", ".join(f"{i['ppnPercentage']}%" for i in items), {"ppn": items})


def cmd_raw(a):
    try:
        variables = json.loads(a.vars) if a.vars else {}
    except json.JSONDecodeError as e:
        raise InaprocError(f"--vars is not valid JSON: {e}")
    d = gql(a.operation, variables)
    a.json = True
    emit(a, "raw", a.operation, d, "", {"data": [d]})


def cmd_ops(a):
    for k in sorted(QUERIES):
        m = re.match(r"query \w+\s*(\([^{]*\))?", QUERIES[k])
        print(f"{k} {' '.join((m.group(1) or '').split()) if m else ''}")


def cmd_engine(a):
    print(json.dumps({"version": VERSION, "engine": CLIENT.engine, "minInterval": CLIENT.limiter.interval,
                      "concurrency": CLIENT.concurrency, "proxy": bool(CLIENT.proxy)}, indent=2))


# ======================================================================= discover (browser)
TRACKERS = ["*google-analytics.com*", "*googletagmanager.com*", "*clarity.ms*", "*facebook.net*",
            "*doubleclick.net*"]


def capture_graphql(tab, urls, wait_s=0):
    """Open each URL in `tab` and return every GraphQL POST the pages send (first seen per operation)."""
    reqs, status = {}, {}

    def sent(p):
        r = p.get("request", {})
        if r.get("method") == "POST" and "/graphql" in r.get("url", ""):
            reqs[p["requestId"]] = {"url": r["url"], "page": p.get("documentURL") or r["url"],
                                    "body": r.get("postData"), "hasBody": r.get("hasPostData")}

    def got(p):
        status[p["requestId"]] = p.get("response", {}).get("status")

    tab.on("Network.requestWillBeSent", sent)
    tab.on("Network.responseReceived", got)
    tab.block_urls(TRACKERS)
    tab.send("Page.enable")
    for u in urls:
        tab.navigate(u)
        tab.wait_load(timeout=60)
        tab.wait_network_idle(quiet_ms=500, timeout=60)
        if wait_s:
            time.sleep(wait_s)
            tab.wait_network_idle(quiet_ms=500, timeout=30)
    found = {}
    for rid, r in reqs.items():
        body = r["body"]
        if body is None and r["hasBody"]:
            try:
                body = tab.send("Network.getRequestPostData", {"requestId": rid}).get("postData")
            except Exception:
                body = None
        try:
            req = json.loads(body or "")
        except ValueError:
            continue
        for item in req if isinstance(req, list) else [req]:
            if not isinstance(item, dict):
                continue
            name = item.get("operationName") or "(anonymous)"
            found.setdefault(name, {"operationName": name, "status": status.get(rid), "page": r["page"],
                                    "query": item.get("query"), "exampleVariables": item.get("variables")})
    return sorted(found.values(), key=lambda o: o["operationName"])


def cmd_discover(a):
    """Open catalogue pages in a never-logged-in Chrome profile, record the site's own GraphQL calls over the
    DevTools Protocol and list (optionally save) every operation seen. Use it to refresh the queries when the
    site changes. Needs a local Chrome, Chromium, Edge or Brave; no pip packages."""
    kw = urllib.parse.quote(a.keyword or "kertas")
    urls = a.urls or [SITE + "/", f"{SITE}/search?keyword={kw}", f"{SITE}/search?keyword={kw}&tabActive=SEARCH_SELLER"]
    for u in urls:
        if not re.match(r"^https://(www\.)?katalog\.inaproc\.id(/|$)", u):
            raise InaprocError(f"discover only opens katalog.inaproc.id pages, not {u}")
    import inaproc_chrome as ic
    try:
        with ic.Browser.start("chrome-public", headed=not a.headless, chrome_path=a.chrome_path, proxy=a.proxy) as b:
            ops = capture_graphql(b.new_tab("about:blank"), urls, wait_s=a.wait)
    except ic.ChromeError as e:
        raise InaprocError(str(e))
    md = (f"**{len(ops)} GraphQL operation(s) captured** from {len(urls)} page(s) "
          "(your Chrome on a separate never-logged-in profile, trackers blocked)\n\n"
          + table(["Operation", "HTTP", "Known to this tool"],
                  [[o["operationName"], o["status"], "yes" if o["operationName"] in QUERIES else "new"] for o in ops]))
    if a.update_queries:
        path = os.path.join(HERE, "queries.discovered.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({o["operationName"]: o["query"] for o in ops if o.get("query")}, f, ensure_ascii=False, indent=1)
        md += f"\n\nWrote {path}. Review it, then merge what you need into queries.json."
    emit(a, "discover", "capture", {"operations": ops}, md,
         {"operations": [{k: v for k, v in o.items() if k != "query"} for o in ops]})


# ======================================================================= RAB comparison (Perbandingan)
_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def _xlsx_cell_ref(ref):
    m = re.match(r"([A-Z]+)(\d+)", ref)
    col = 0
    for ch in m.group(1):
        col = col * 26 + ord(ch) - 64
    return col, int(m.group(2))


def read_xlsx(path, sheet=None):
    """Standard-library .xlsx reader: {sheet name: [[cell values]]} (values only, formulas give cached results)."""
    import xml.etree.ElementTree as ET
    ns = {"m": _NS_MAIN, "r": _NS_REL}
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS_MAIN}}}t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        out = {}
        for s in wb.find("m:sheets", ns):
            name = s.get("name")
            if sheet and name.lower() != str(sheet).lower():
                continue
            target = rels[s.get(f"{{{_NS_REL}}}id")].lstrip("/")
            target = target if target.startswith("xl/") else "xl/" + target
            rows = {}
            for c in ET.fromstring(z.read(target)).iter(f"{{{_NS_MAIN}}}c"):
                col, row = _xlsx_cell_ref(c.get("r"))
                t, v = c.get("t"), c.find("m:v", ns)
                if t == "s" and v is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{{{_NS_MAIN}}}t"))
                elif v is None or v.text is None:
                    continue
                elif t in ("str", "e"):
                    val = v.text
                elif t == "b":
                    val = v.text == "1"
                else:
                    try:
                        f = float(v.text)
                        val = int(f) if f.is_integer() else f
                    except ValueError:
                        val = v.text
                if val == "":
                    continue
                rows.setdefault(row, {})[col] = val
            grid = []
            for r in range(1, max(rows, default=0) + 1):
                cells = rows.get(r, {})
                grid.append([cells.get(i) for i in range(1, max(cells, default=0) + 1)])
            out[name] = grid
        if sheet and not out:
            raise InaprocError(f"sheet {sheet!r} not found in {path}")
        return out


def cmd_read_xlsx(a):
    if not os.path.exists(a.file):
        raise InaprocError(f"file not found: {a.file}")
    data = read_xlsx(a.file, a.sheet)
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return
    for name, grid in data.items():
        print(f"## Sheet: {name} ({len(grid)} rows)\n")
        width = max((len(r) for r in grid), default=0)
        if not width:
            print("_empty_\n")
            continue
        print(table(["Row"] + [_col(i) for i in range(1, width + 1)],
                    [[i] + [cell(v, 50) for v in r] + [""] * (width - len(r))
                     for i, r in enumerate(grid[: a.max_rows], 1) if any(v not in (None, "") for v in r)]))
        if len(grid) > a.max_rows:
            print(f"\n… {len(grid) - a.max_rows} more rows (use --max-rows)")
        print()


# Group colours of the "Perbandingan" sheet: RAB, company, alternatives, conclusion.
_PB_RAB, _PB_COMPANY, _PB_CONCL = "44546A", "C55A11", "7030A0"
_PB_ALTS = ["2E75B6", "548235", "7F6000", "833C0B", "1F4E78"]
_PB_WIN_FILL = "E2EFDA"


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v
    s = re.sub(r"[^\d,.\-]", "", str(v))
    if re.search(r"\d\.\d{3}(\.|$)", s) or ("," in s and "." in s and s.rfind(",") > s.rfind(".")):
        s = s.replace(".", "").replace(",", ".")  # Indonesian 1.234.567,89
    s = s.replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def _excluded(alt):
    return bool(alt.get("exclude")) or str(alt.get("kesesuaian") or "").strip().lower().startswith("berbeda")


def build_perbandingan(spec):
    """Normalise a comparison spec and compute totals, the per-item conclusion and warnings."""
    mode = (spec.get("mode") or "cheapest").lower()
    if mode not in ("win", "cheapest"):
        raise InaprocError("spec.mode must be 'win' (company wins) or 'cheapest' (find the cheapest)")
    comp = spec.get("company") or {}
    cname = comp.get("name") or "PERUSAHAAN"
    loc = ", ".join(x for x in (comp.get("city"), comp.get("province")) if x)
    clabel = comp.get("label") or (f"{cname.upper()} ({loc})" if loc else cname.upper())
    items = spec.get("items") or []
    if not items:
        raise InaprocError("spec.items is empty: list every RAB line")
    n_alt = max(int(spec.get("alternatives", 2) or 2), max(len(i.get("alternatives") or []) for i in items), 2)
    warnings, rows = [], []
    for idx, it in enumerate(items, 1):
        no = it.get("no", idx)
        qty = _num(it.get("qty")) or 0
        rab = _num(it.get("harga_rab"))
        c = dict(it.get("company") or {})
        c["harga_ppn"], c["harga_sblm_ppn"] = _num(c.get("harga_ppn")), _num(c.get("harga_sblm_ppn"))
        alts = []
        for alt in (it.get("alternatives") or [])[:n_alt]:
            alt = dict(alt)
            alt["harga_ppn"] = _num(alt.get("harga_ppn"))
            alts.append(alt)
        label = f"item {no} ({it.get('uraian') or '-'})"
        if not c.get("url") and c.get("harga_ppn") is not None:
            warnings.append(f"{label}: company price has no product URL")
        for k, alt in enumerate(alts, 1):
            if not alt.get("url"):
                warnings.append(f"{label}: alternative {k} has no product URL")
            if alt.get("harga_ppn") is None:
                warnings.append(f"{label}: alternative {k} has no price")
        usable = [a_ for a_ in alts if not _excluded(a_) and a_.get("harga_ppn") is not None]
        if len(alts) < 2:
            warnings.append(f"{label}: only {len(alts)} comparison product(s); at least 2 are required")
        cp = c.get("harga_ppn")
        if mode == "win":
            if cp is None:
                warnings.append(f"{label}: no company product/price, the company cannot win this line")
            else:
                for k, alt in enumerate(alts, 1):
                    if alt.get("harga_ppn") is not None and alt["harga_ppn"] <= cp:
                        warnings.append(f"{label}: alternative {k} ({alt.get('penyedia')}) Rp{alt['harga_ppn']:,.0f} "
                                        f"is not more expensive than the company Rp{cp:,.0f}".replace(",", "."))
        # conclusion: the cheapest comparable offer (company included), unless the spec sets one
        cands = ([(cp, cname, True)] if cp is not None else []) + \
                [(a_["harga_ppn"], a_.get("penyedia") or "-", False) for a_ in usable]
        concl = dict(it.get("kesimpulan") or {})
        if cands and not concl.get("penyedia"):
            best = min(cands, key=lambda x: (x[0], not x[2]))
            concl.setdefault("penyedia", best[1])
            concl.setdefault("harga_ppn", best[0])
            concl["company_wins"] = best[2]
        else:
            concl["harga_ppn"] = _num(concl.get("harga_ppn"))
            concl["company_wins"] = (concl.get("penyedia") or "").strip().lower() == cname.strip().lower()
        concl["jumlah"] = qty * concl["harga_ppn"] if concl.get("harga_ppn") is not None else None
        concl["catatan"] = it.get("catatan") or concl.get("catatan") or ""
        rows.append({"no": no, "uraian": it.get("uraian"), "ukuran": it.get("ukuran") or "-", "qty": qty,
                     "satuan": it.get("satuan"), "harga_rab": rab,
                     "jumlah_rab": qty * rab if rab is not None else None,
                     "company": c, "alternatives": alts, "kesimpulan": concl})
    tot = {"rab": sum(r["jumlah_rab"] or 0 for r in rows),
           "company": sum(r["qty"] * r["company"]["harga_ppn"] for r in rows if r["company"].get("harga_ppn") is not None),
           "termurah": sum(r["kesimpulan"]["jumlah"] or 0 for r in rows),
           "alternatives": [sum(r["qty"] * r["alternatives"][k]["harga_ppn"] for r in rows
                                if len(r["alternatives"]) > k and r["alternatives"][k].get("harga_ppn") is not None)
                            for k in range(n_alt)],
           "company_wins": sum(1 for r in rows if r["kesimpulan"].get("company_wins"))}
    return {"title": spec.get("title") or "Perbandingan RAB", "mode": mode, "company": dict(comp, name=cname, label=clabel),
            "n_alt": n_alt, "items": rows, "totals": tot, "warnings": warnings}


def _pb_styles(n_alt):
    """styles.xml for the Perbandingan sheet; returns (xml, style index map)."""
    group_fills = [_PB_RAB, _PB_COMPANY] + [_PB_ALTS[k % len(_PB_ALTS)] for k in range(n_alt)] + [_PB_CONCL]
    fills = ['<fill><patternFill patternType="none"/></fill>', '<fill><patternFill patternType="gray125"/></fill>']
    fills += [f'<fill><patternFill patternType="solid"><fgColor rgb="FF{c}"/></patternFill></fill>'
              for c in group_fills + [_PB_WIN_FILL]]
    win_fill = len(fills) - 1
    fonts = ('<fonts count="4"><font><sz val="11"/><color theme="1"/><name val="Calibri"/><family val="2"/>'
             '<scheme val="minor"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
             '<font><u/><sz val="11"/><color rgb="FF0563C1"/><name val="Calibri"/></font>'
             '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>')
    border = ('<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border>'
              + "".join(f'<{s} style="thin"><color rgb="FFBFBFBF"/></{s}>' for s in ("left", "right", "top", "bottom"))
              + "<diagonal/></border></borders>")
    xfs, st = ['<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'], {}

    def add(key, num=0, font=0, fill=0, bord=0, align=""):
        st[key] = len(xfs)
        xfs.append(f'<xf numFmtId="{num}" fontId="{font}" fillId="{fill}" borderId="{bord}" xfId="0" applyNumberFormat="1"'
                   f' applyFont="1" applyFill="1" applyBorder="1"' + (f' applyAlignment="1"><alignment {align}/></xf>'
                                                                       if align else "/>"))
    for g in range(len(group_fills)):
        add(("group", g), font=1, fill=2 + g, align='horizontal="center"')
        add(("head", g), font=1, fill=2 + g, bord=1, align='horizontal="center" vertical="center" wrapText="1"')
    top = 'vertical="top" wrapText="1"'
    add("text", bord=1, align=top)
    add("money", num=164, bord=1, align=top)
    add("link", font=2, bord=1, align=top)
    add("win", fill=win_fill, bord=1, align=top)
    add("total", num=164, font=3, bord=1)
    add("bold", font=3)
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<styleSheet xmlns="{_NS_MAIN}"><numFmts count="1"><numFmt numFmtId="164" formatCode="&quot;Rp&quot;#,##0"/>'
           f'</numFmts>{fonts}<fills count="{len(fills)}">{"".join(fills)}</fills>{border}'
           '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
           f'<cellXfs count="{len(xfs)}">{"".join(xfs)}</cellXfs>'
           '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    return xml, st


def write_perbandingan_xlsx(path, rep):
    """One-sheet workbook, sheet "Perbandingan", laid out like the reference comparison workbook."""
    n_alt = rep["n_alt"]
    xml_styles, st = _pb_styles(n_alt)
    groups = [("KEBUTUHAN RAB", ["No", "Uraian", "Ukuran", "Qty", "Satuan", "Harga Satuan RAB", "Jumlah RAB"],
               [5, 34, 14, 6, 8, 13, 14]),
              (rep["company"]["label"], ["Varian", "Harga sblm PPN", "Harga incl. PPN", "Jumlah incl. PPN",
                                                 "Selisih vs RAB", "URL"], [20, 13, 13, 14, 13, 30])]
    groups += [(f"ALTERNATIF {k + 1}", ["Penyedia", "Kota", "Spesifikasi / Ukuran", "Kesesuaian", "Satuan",
                                        "Harga incl. PPN", "Jumlah incl. PPN", "URL"], [20, 14, 36, 20, 9, 13, 14, 30])
               for k in range(n_alt)]
    groups.append(("KESIMPULAN", ["Termurah (sebanding)", "Jumlah termurah", "Catatan"], [22, 14, 45]))
    start, col = [], 1
    for g in groups:
        start.append(col)
        col += len(g[1])
    last_col = col - 1
    widths = [w for g in groups for w in g[2]]

    cells_by_row, links, merges = {}, [], []

    def put(r, c, v, style, formula=None):
        ref = f"{_col(c)}{r}"
        s = f' s="{st[style]}"' if style is not None else ""
        if formula:
            cv = "" if v is None else f"<v>{v}</v>"
            x = f'<c r="{ref}"{s}><f>{_xml_escape(formula)}</f>{cv}</c>'
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            x = f'<c r="{ref}"{s}><v>{v}</v></c>'
        elif v is None or v == "":
            x = f'<c r="{ref}"{s}/>'
        else:
            t = _xml_escape(_ILLEGAL_XML.sub("", str(v))[:32767])
            x = f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{t}</t></is></c>'
        cells_by_row.setdefault(r, []).append((c, x))
        return ref

    for g, (title, heads, _) in enumerate(groups):
        c0 = start[g]
        put(1, c0, title, ("group", g))
        for k in range(1, len(heads)):
            put(1, c0 + k, None, ("group", g))
        merges.append(f"{_col(c0)}1:{_col(c0 + len(heads) - 1)}1")
        for k, h in enumerate(heads):
            put(2, c0 + k, h, ("head", g))

    def money(v):
        return v if v is not None else None

    first, r = 3, 2
    for it in rep["items"]:
        r += 1
        C = lambda c: f"{_col(c)}{r}"  # noqa: E731
        q = it["qty"]
        put(r, 1, it["no"], "text")
        put(r, 2, it["uraian"], "text")
        put(r, 3, it["ukuran"], "text")
        put(r, 4, q, "text")
        put(r, 5, it["satuan"], "text")
        put(r, 6, money(it["harga_rab"]), "money")
        put(r, 7, it["jumlah_rab"], "money", f"{C(4)}*{C(6)}" if it["harga_rab"] is not None else None)
        c = it["company"]
        b = start[1]
        put(r, b, c.get("varian"), "text")
        put(r, b + 1, money(c.get("harga_sblm_ppn")), "money")
        put(r, b + 2, money(c.get("harga_ppn")), "money")
        if c.get("harga_ppn") is not None:
            put(r, b + 3, q * c["harga_ppn"], "money", f"{C(4)}*{C(b + 2)}")
            put(r, b + 4, q * c["harga_ppn"] - (it["jumlah_rab"] or 0), "money", f"{C(b + 3)}-{C(7)}")
        else:
            put(r, b + 3, None, "money")
            put(r, b + 4, None, "money")
        if c.get("url"):
            links.append((put(r, b + 5, c["url"], "link"), c["url"]))
        else:
            put(r, b + 5, None, "text")
        for k in range(n_alt):
            b = start[2 + k]
            alt = it["alternatives"][k] if k < len(it["alternatives"]) else {}
            put(r, b, alt.get("penyedia"), "text")
            put(r, b + 1, alt.get("kota"), "text")
            put(r, b + 2, alt.get("spesifikasi"), "text")
            put(r, b + 3, alt.get("kesesuaian"), "text")
            put(r, b + 4, alt.get("satuan"), "text")
            put(r, b + 5, money(alt.get("harga_ppn")), "money")
            put(r, b + 6, q * alt["harga_ppn"] if alt.get("harga_ppn") is not None else None, "money",
                f"{C(4)}*{C(b + 5)}" if alt.get("harga_ppn") is not None else None)
            if alt.get("url"):
                links.append((put(r, b + 7, alt["url"], "link"), alt["url"]))
            else:
                put(r, b + 7, None, "text")
        b, k = start[-1], it["kesimpulan"]
        # highlight the outcome the user asked for: the company winning (win) or a cheaper offer (cheapest)
        good = k.get("company_wins") if rep["mode"] == "win" else (k.get("penyedia") and not k.get("company_wins"))
        put(r, b, k.get("penyedia"), "win" if good else "text")
        put(r, b + 1, k.get("jumlah"), "money")
        put(r, b + 2, k.get("catatan"), "text")
    last = r
    r += 1
    put(r, 2, "TOTAL", "bold")
    sum_cols = [(7, rep["totals"]["rab"]), (start[1] + 3, rep["totals"]["company"]),
                (start[1] + 4, rep["totals"]["company"] - rep["totals"]["rab"])]
    sum_cols += [(start[2 + k] + 6, rep["totals"]["alternatives"][k]) for k in range(n_alt)]
    sum_cols.append((start[-1] + 1, rep["totals"]["termurah"]))
    for cidx, val in sum_cols:
        put(r, cidx, val, "total", f"SUM({_col(cidx)}{first}:{_col(cidx)}{last})")

    sheet = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             f'<worksheet xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">',
             f'<dimension ref="A1:{_col(last_col)}{r}"/>',
             '<sheetViews><sheetView workbookViewId="0"><pane xSplit="2" ySplit="2" topLeftCell="C3" '
             'activePane="bottomRight" state="frozen"/><selection pane="topRight"/><selection pane="bottomLeft"/>'
             '<selection pane="bottomRight" activeCell="A1" sqref="A1"/></sheetView></sheetViews>',
             '<sheetFormatPr baseColWidth="8" defaultRowHeight="15"/><cols>',
             "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths, 1)),
             "</cols><sheetData>"]
    for rr in sorted(cells_by_row):
        attrs = ' ht="32" customHeight="1"' if rr == 2 else ""
        sheet.append(f'<row r="{rr}"{attrs}>' + "".join(x for _, x in sorted(cells_by_row[rr])) + "</row>")
    sheet.append("</sheetData>")
    sheet.append(f'<autoFilter ref="A2:{_col(last_col)}{last}"/>')
    sheet.append(f'<mergeCells count="{len(merges)}">' + "".join(f'<mergeCell ref="{m}"/>' for m in merges)
                 + "</mergeCells>")
    if links:
        sheet.append("<hyperlinks>" + "".join(f'<hyperlink ref="{ref}" r:id="rId{i}"/>'
                                              for i, (ref, _) in enumerate(links, 1)) + "</hyperlinks>")
    sheet.append('<pageMargins left="0.75" right="0.75" top="1" bottom="1" header="0.5" footer="0.5"/></worksheet>')

    xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", xml +
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                   "</Types>")
        z.writestr("_rels/.rels", xml + f'<Relationships xmlns="{_NS_PKG}">'
                   f'<Relationship Id="rId1" Type="{_NS_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", xml + f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}"><sheets>'
                   '<sheet name="Perbandingan" sheetId="1" r:id="rId1"/></sheets><definedNames>'
                   f'<definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'
                   f"'Perbandingan'!$A$2:${_col(last_col)}${last}</definedName></definedNames>"
                   '<calcPr calcId="124519" fullCalcOnLoad="1"/></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", xml + f'<Relationships xmlns="{_NS_PKG}">'
                   f'<Relationship Id="rId1" Type="{_NS_REL}/worksheet" Target="worksheets/sheet1.xml"/>'
                   f'<Relationship Id="rId2" Type="{_NS_REL}/styles" Target="styles.xml"/></Relationships>')
        z.writestr("xl/styles.xml", xml_styles)
        z.writestr("xl/worksheets/sheet1.xml", "".join(sheet))
        if links:
            z.writestr("xl/worksheets/_rels/sheet1.xml.rels", xml + f'<Relationships xmlns="{_NS_PKG}">'
                       + "".join(f'<Relationship Id="rId{i}" Type="{_NS_REL}/hyperlink" '
                                 f'Target="{_xml_escape(u, {chr(34): "&quot;"})}" TargetMode="External"/>'
                                 for i, (_, u) in enumerate(links, 1)) + "</Relationships>")


def _pb_flat_rows(rep):
    """The Perbandingan sheet as flat rows (CSV): one row per RAB line, group-prefixed column names."""
    cl = rep["company"]["name"]
    out = []
    for it in rep["items"]:
        c, k = it["company"], it["kesimpulan"]
        row = {"No": it["no"], "Uraian": it["uraian"], "Ukuran": it["ukuran"], "Qty": it["qty"],
               "Satuan": it["satuan"], "Harga Satuan RAB": it["harga_rab"], "Jumlah RAB": it["jumlah_rab"],
               f"{cl} - Varian": c.get("varian"), f"{cl} - Harga sblm PPN": c.get("harga_sblm_ppn"),
               f"{cl} - Harga incl. PPN": c.get("harga_ppn"),
               f"{cl} - Jumlah incl. PPN": it["qty"] * c["harga_ppn"] if c.get("harga_ppn") is not None else None,
               f"{cl} - Selisih vs RAB": (it["qty"] * c["harga_ppn"] - (it["jumlah_rab"] or 0))
               if c.get("harga_ppn") is not None else None, f"{cl} - URL": c.get("url")}
        for n in range(rep["n_alt"]):
            alt = it["alternatives"][n] if n < len(it["alternatives"]) else {}
            p = f"Alternatif {n + 1} - "
            row.update({p + "Penyedia": alt.get("penyedia"), p + "Kota": alt.get("kota"),
                        p + "Spesifikasi / Ukuran": alt.get("spesifikasi"), p + "Kesesuaian": alt.get("kesesuaian"),
                        p + "Satuan": alt.get("satuan"), p + "Harga incl. PPN": alt.get("harga_ppn"),
                        p + "Jumlah incl. PPN": it["qty"] * alt["harga_ppn"] if alt.get("harga_ppn") is not None else None,
                        p + "URL": alt.get("url")})
        row.update({"Termurah (sebanding)": k.get("penyedia"), "Jumlah termurah": k.get("jumlah"),
                    "Catatan": k.get("catatan")})
        out.append(row)
    return out


def _verify_spec(spec):
    """Re-read every product URL in the spec from the catalogue: fill missing prices/cities, flag stale prices."""
    refs, notes = [], []
    for it in spec.get("items") or []:
        for o in [it.get("company") or {}] + list(it.get("alternatives") or []):
            if o.get("url") and o["url"] not in refs:
                refs.append(o["url"])
    if not refs:
        return notes
    print(f"verifying {len(refs)} product URL(s) on {SITE} …", file=sys.stderr)
    found = {}
    calls = []
    for ref in refs:
        try:
            u, s = parse_product_ref(ref)
        except InaprocError as e:
            notes.append(str(e))
            continue
        calls.append((ref, ("getProductBySlug", {"username": u, "slug": s, "regionCode": None})))
    for (ref, _), d in zip(calls, CLIENT.gql_many([c for _, c in calls])):
        p = d.get("getProductBySlug")
        if p:
            found[ref] = slim_detail(p)
        else:
            notes.append(f"product not found (removed or wrong URL): {ref}")
    for it in spec.get("items") or []:
        for o in [it.get("company") or {}] + list(it.get("alternatives") or []):
            d = found.get(o.get("url"))
            if not d:
                continue
            if not o.get("kota") and d.get("sellerCity"):
                o["kota"] = d["sellerCity"].title()
            if not o.get("satuan") and d.get("unit"):
                o["satuan"] = d["unit"]
            price = _num(o.get("harga_ppn"))
            vs = [v for v in d["variants"] if v.get("priceWithTax") is not None]
            if price is None:
                notes.append(f"{o['url']}: no harga_ppn in spec; catalogue range "
                             f"{price_range(d['priceMinWithTax'], d['priceMaxWithTax'])}")
                continue
            match = [v for v in vs if abs(v["priceWithTax"] - price) < 1]
            if match:
                if o is it.get("company") and o.get("harga_sblm_ppn") in (None, ""):
                    o["harga_sblm_ppn"] = match[0]["price"]
            elif not vs or not (d["priceMinWithTax"] or 0) - 1 <= price <= (d["priceMaxWithTax"] or 0) + 1:
                notes.append(f"{o['url']}: spec price {rp(price)} not on the catalogue now "
                             f"({price_range(d['priceMinWithTax'], d['priceMaxWithTax'])})")
    return notes


def cmd_rab_report(a):
    if not os.path.exists(a.spec):
        raise InaprocError(f"spec file not found: {a.spec}")
    with open(a.spec, encoding="utf-8") as f:
        spec = json.load(f)
    if a.mode:
        spec["mode"] = a.mode
    notes = _verify_spec(spec) if a.verify else []
    rep = build_perbandingan(spec)
    rep["warnings"] = notes + rep["warnings"]
    t, comp = rep["totals"], rep["company"]["name"]
    head = ["No", "Uraian", "Qty", "RAB", comp] + [f"Alt {k + 1}" for k in range(rep["n_alt"])] + ["Termurah"]
    body = []
    for it in rep["items"]:
        alts = it["alternatives"]
        body.append([it["no"], it["uraian"], it["qty"], rp(it["harga_rab"]), rp(it["company"].get("harga_ppn"))]
                    + [f"{rp(alts[k].get('harga_ppn'))} {alts[k].get('penyedia') or ''}".strip() if k < len(alts) else "-"
                       for k in range(rep["n_alt"])]
                    + [f"{it['kesimpulan'].get('penyedia') or '-'}"])
    lines = [f"## {rep['title']}", f"Mode: **{'perusahaan menang' if rep['mode'] == 'win' else 'cari termurah'}**", "",
             table(head, body), "",
             table(["Total", "Rupiah"], [["RAB", rp(t["rab"])], [f"{comp} (incl. PPN)", rp(t["company"])]]
                   + [[f"Alternatif {k + 1}", rp(v)] for k, v in enumerate(t["alternatives"])]
                   + [["Termurah sebanding", rp(t["termurah"])],
                      [f"Baris yang dimenangkan {comp}", f"{t['company_wins']} / {len(rep['items'])}"]])]
    links = []
    for it in rep["items"]:
        for o in [it["company"]] + it["alternatives"]:
            if o.get("url") and o["url"] not in links:
                links.append(o["url"])
    lines += ["", "**Link produk**", ""] + [f"- {u}" for u in links]
    if rep["warnings"]:
        lines += ["", "**Perlu dicek**", ""] + [f"- {w}" for w in rep["warnings"]]
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))
    if a.save:
        target = os.path.join(os.path.abspath(os.path.expanduser(a.out_dir)), "output")
        os.makedirs(target, exist_ok=True)
        now = _dt.datetime.now().astimezone()
        stem = f"perbandingan_{slugify(rep['title'])}_{now.strftime('%Y%m%d-%H%M%S')}"
        meta = {"source": SITE, "command": "rab-report", "query": rep["title"], "generatedAt": now.isoformat(timespec="seconds")}
        written = []
        for fmt in a.save:
            p = os.path.join(target, f"{stem}.{fmt}")
            if fmt == "xlsx":
                write_perbandingan_xlsx(p, rep)
            elif fmt == "json":
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({"meta": meta, "data": rep}, f, ensure_ascii=False, indent=2)
            elif fmt == "yaml":
                with open(p, "w", encoding="utf-8") as f:
                    f.write(to_yaml({"meta": meta, "data": rep}) + "\n")
            elif fmt == "csv":
                rows = _pb_flat_rows(rep)
                with open(p, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(rows[0]))
                    w.writeheader()
                    w.writerows(rows)
            written.append(p)
        print("\nSaved:\n" + "\n".join(f"- {p}" for p in written))


# ======================================================================= CLI
def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("output")
    g.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    g.add_argument("--save", type=lambda s: [f.strip().lower() for f in s.split(",") if f.strip()],
                   help="save files: comma list of json,csv,xlsx,yaml")
    g.add_argument("--out-dir", help="base folder; files go to <out-dir>/output/ (required with --save)")
    n = common.add_argument_group("network")
    n.add_argument("--engine", choices=["auto", "stdlib", "scrapling"], default=os.environ.get("INAPROC_ENGINE", "auto"),
                   help="HTTP engine (auto and stdlib both use the built-in engine)")
    n.add_argument("--impersonate", default=None, help=argparse.SUPPRESS)
    n.add_argument("--http3", action="store_true", help=argparse.SUPPRESS)
    n.add_argument("--proxy", default=os.environ.get("INAPROC_PROXY"), help="http(s)://user:pass@host:port")
    n.add_argument("--min-interval", type=float, default=float(os.environ.get("INAPROC_MIN_INTERVAL", "1.0")),
                   help=f"seconds between request starts (floor {MIN_INTERVAL_FLOOR})")
    n.add_argument("--concurrency", type=int, default=2, help=f"parallel requests, 1–{MAX_CONCURRENCY}")

    p = argparse.ArgumentParser(prog="inaproc", description="Search katalog.inaproc.id (public data, no login), "
                                "and manage your own penyedia products (`penyedia`).")
    p.add_argument("--version", action="version", version=f"inaproc {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_, parents=[common])
        s.set_defaults(fn=fn)
        return s

    def filters(sp):
        sp.add_argument("--type", action="append", choices=sorted(TYPES), help="barang | jasa | digital (repeatable)")
        sp.add_argument("--label", action="append", help="product label, e.g. PDN (repeatable)")
        sp.add_argument("--seller-type", action="append", help="umkk | official | oap (repeatable)")
        sp.add_argument("--min-price", type=float, help="minimum price in Rupiah")
        sp.add_argument("--max-price", type=float, help="maximum price in Rupiah")
        sp.add_argument("--category", action="append", help="category id (see `categories`)")
        sp.add_argument("--seller-id", help="only this seller's products (Seller ID from `seller`)")

    s = add("search", cmd_search, "search products")
    s.add_argument("keyword", nargs="?", default=None)
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--pages", type=int, default=1, help="fetch this many consecutive pages")
    s.add_argument("--per-page", type=int, default=20)
    s.add_argument("--sort", choices=sorted(SORTS), default="relevance")
    filters(s)
    s.add_argument("--no-categories", action="store_true", help="skip the Level 2/3 category lookups (faster)")

    s = add("cheapest", cmd_cheapest, "cheapest matching products (relevance first, then by price)")
    s.add_argument("keyword")
    s.add_argument("--pages", type=int, default=2, help="relevance pages of 60 to consider (default 2)")
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--strict", action="store_true", help="product name must contain every keyword")
    s.add_argument("--include-outliers", action="store_true", help="keep suspiciously cheap listings")
    s.add_argument("--outlier-ratio", type=float, default=0.2, help="set aside prices below this share of the median")
    s.add_argument("--no-categories", action="store_true", help="skip the Level 2/3 category lookups (faster)")
    filters(s)

    s = add("detail", cmd_detail, "full detail of one product")
    s.add_argument("product")
    s.add_argument("--region")

    s = add("compare", cmd_compare, "compare 2+ products side by side")
    s.add_argument("products", nargs="+")
    s.add_argument("--region")

    s = add("reviews", cmd_reviews, "ratings and reviews of a product")
    s.add_argument("product")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--per-page", type=int, default=10)

    s = add("seller", cmd_seller, "seller profile, score and products")
    s.add_argument("seller")
    s.add_argument("--products", type=int, default=0, help="also list its top N products by units sold")

    s = add("sellers", cmd_sellers, "search sellers by keyword")
    s.add_argument("keyword")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--per-page", type=int, default=10)

    s = add("categories", cmd_categories, "category tree")
    s.add_argument("--type", default="barang", choices=sorted(TYPES))
    s.add_argument("--depth", type=int, default=2, choices=[1, 2, 3])

    s = add("category", cmd_category, "one category and its parents")
    s.add_argument("id")

    s = add("regions", cmd_regions, "provinces, or cities of a province")
    s.add_argument("--province")

    s = add("region-search", cmd_region_search, "find a region by name")
    s.add_argument("name")
    s.add_argument("--level", default="CITY")
    s.add_argument("--per-page", type=int, default=10)

    s = add("kbli", cmd_kbli, "KBLI business classification codes")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--per-page", type=int, default=50)

    add("ppn", cmd_ppn, "PPN (VAT) rates")

    s = add("raw", cmd_raw, "run any bundled public operation with JSON variables")
    s.add_argument("operation")
    s.add_argument("--vars")

    s = add("read-xlsx", cmd_read_xlsx, "print the cells of a local .xlsx file (e.g. a RAB) without extra packages")
    s.add_argument("file")
    s.add_argument("--sheet", help="only this sheet")
    s.add_argument("--max-rows", type=int, default=300)

    s = add("rab-report", cmd_rab_report,
            "render a RAB comparison spec (JSON) as the one-sheet 'Perbandingan' workbook")
    s.add_argument("spec", help="path to the comparison spec JSON (see references/rab-comparison.md)")
    s.add_argument("--mode", choices=["win", "cheapest"], help="override spec.mode")
    s.add_argument("--verify", action="store_true",
                   help="re-read every product URL on the catalogue (1 call per unique URL) and flag stale prices")

    import inaproc_penyedia
    inaproc_penyedia.add_parsers(sub, common)

    import inaproc_update
    s = add("update", inaproc_update.cmd_update, "update this installed skill from GitHub now (it also checks daily)")
    s.add_argument("--check", action="store_true", help="only say whether a newer version exists")

    add("ops", cmd_ops, "list bundled public operations")
    add("engine", cmd_engine, "show which HTTP engine and settings will be used")

    s = add("discover", cmd_discover, "capture the site's API calls in your own Chrome (DevTools Protocol)")
    s.add_argument("urls", nargs="*", help="katalog.inaproc.id pages to open (default: home + searches)")
    s.add_argument("--keyword", help="search word for the default search pages")
    s.add_argument("--headed", action="store_true", help=argparse.SUPPRESS)  # v1.0 flag; a window is the default now
    s.add_argument("--headless", action="store_true",
                   help="no window (the live site's Cloudflare refuses headless Chrome; for local testing)")
    s.add_argument("--wait", type=float, default=0,
                   help="seconds to keep each page open (you can click around in the window; "
                        "first loads are server-rendered, most API calls happen on interaction)")
    s.add_argument("--update-queries", action="store_true", help="write scripts/queries.discovered.json")
    s.add_argument("--chrome-path", help="Chrome/Chromium/Edge/Brave binary (default: found automatically, "
                                         "or $INAPROC_CHROME)")
    return p


def compat_warnings(args):
    """Options kept from v1.0 so old scripts keep running; each one only prints a warning now."""
    out = []
    if getattr(args, "engine", None) == "scrapling":
        out.append("Scrapling support was removed in v1.1; using the built-in engine")
    if getattr(args, "impersonate", None):
        out.append("--impersonate is ignored since v1.1 (Scrapling support was removed)")
    if getattr(args, "http3", False):
        out.append("--http3 is ignored since v1.1 (Scrapling support was removed)")
    return out


def main(argv=None):
    global CLIENT
    args = build_parser().parse_args(argv)
    if args.save:
        bad = [f for f in args.save if f not in FORMATS]
        if bad:
            print(f"error: unknown format(s) {', '.join(bad)}; choose from {', '.join(FORMATS)}", file=sys.stderr)
            return 2
        if not args.out_dir:
            print("error: --save needs --out-dir (ask the user where to save; files go to <out-dir>/output/)",
                  file=sys.stderr)
            return 2
    for w in compat_warnings(args):
        print(f"warning: {w}", file=sys.stderr)
    try:
        offline = args.cmd in ("ops", "discover", "read-xlsx", "penyedia", "update") or (args.cmd == "rab-report" and not args.verify)
        if not offline:
            CLIENT = Client(proxy=args.proxy, min_interval=args.min_interval, concurrency=args.concurrency)
        args.fn(args)
        if args.cmd != "update":
            import inaproc_update
            note = inaproc_update.maybe_update(VERSION)  # at most once a day; silent on any problem
            if note:
                print(f"\n{note}", file=sys.stderr)
    except InaprocError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        code = getattr(e, "exit_code", None)
        if code is None and type(e).__name__ != "ChromeError":
            raise
        print(f"error: {e}", file=sys.stderr)
        return code or 1
    finally:
        if CLIENT:
            CLIENT.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
