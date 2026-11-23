# Chrome engine, Scrapling removal, and penyedia (seller) product commands — design

Date: 2026-11-23 · Repo: `inaproc-skill/` · Skill source: `skills/inaproc/`

## 1. Intent

The `inaproc` skill today reads the public catalogue (katalog.inaproc.id) with Python's standard
library, and optionally uses Scrapling (pip package + Playwright browser) for a browser TLS fingerprint
and for the `discover` maintenance command. The owner wants:

1. **Zero installs.** Drop Scrapling. Rebuild what can be rebuilt in plain Python: browser-style
   request headers, and `discover` driven through the user's own Chrome over the DevTools Protocol (CDP).
2. **Manage their own seller (penyedia) products** on `penyedia.inaproc.id` (`/products`,
   `/products/add`): list, read, create and update products, including bulk updates from CSV/XLSX,
   using their logged-in session. These writes go live on a government catalogue, so every write must
   be shown first, confirmed, and logged.

Single account: the owner's own penyedia account only.

## 2. Goals, non-goals, anti-goals

**Goals**
- G1. Standard library only (Python 3.8+), on macOS, Linux and Windows. Chrome/Chromium/Edge/Brave
  installed is the only external requirement, and only for `discover` and `penyedia` commands.
- G2. Default engine sends a coherent Chrome-like header set in Chrome's order.
- G3. `discover` works through a skill-owned Chrome profile, with the same output as today.
- G4. Owner logs in once in a visible Chrome window; later commands reuse that login without the
  password ever passing through the skill.
- G5. A recorder captures the seller site's real API traffic while the owner works by hand, so the
  seller commands are built from observed requests, not guesses.
- G6. Seller commands: list, get, create, update, bulk update. Writes: dry run by default, exact
  plan executed only on explicit send, before/after diff for updates, append-only write log.

**Non-goals**
- Matching Chrome's TLS/HTTP2 fingerprint from Python (impossible without compiled code; the 503s seen
  so far are per-IP rate limits, which the 1 req/s limiter already handles).
- DNS-over-HTTPS (was a Scrapling option; Chrome's own setting covers it if the user wants it).
- Multiple penyedia accounts, account switching.
- Penyedia features on claude.ai, Claude Desktop uploads, or the ChatGPT Action. They need a local
  Chrome; the public search features keep working there unchanged.

**Anti-goals (failure even if it "works")**
- The skill reads, stores, asks for or types the owner's password, or automates the login/Turnstile.
- Any write reaches the site without the owner having seen that exact payload and said yes.
- Deleting a product without a dry run and an `apply` of that exact plan, or touching orders, negotiations, payments, contracts or any other mutation
  outside product create/update/delete.
- Tokens or cookies written to files outside Chrome's own profile, or printed in logs/recordings.
- Bursting the site (more than 1 request/second, or parallel writes).

## 3. Constraints and facts found

- `scripts/inaproc.py` is one 1,623-line stdlib file; `queries.json` beside it. No test suite; only the
  `ppn` smoke check in `install.sh`.
- The existing `seller` command means *public seller profile lookup*. New commands must not reuse that
  name → new command group **`penyedia`**.
- `urllib` title-cases header names and puts `Host`/`Accept-Encoding: identity` first; exact ordering
  requires `http.client` directly. Chrome advertises `br`/`zstd`, which have no stdlib decoder → send
  `accept-encoding: gzip, deflate` only.
- **Chrome 136+ ignores `--remote-debugging-port` on the default profile.** The skill must run Chrome
  with its own `--user-data-dir`. Reading the normal profile's cookie DB is out (Keychain/DPAPI-
  encrypted, AES not in stdlib).
- Login is NextAuth-style via `daftar-akun.inaproc.id` (`/api/auth/session`, `/api/auth/refresh`) behind
  Cloudflare Turnstile. How penyedia requests carry auth (cookie vs bearer header) is **unknown until
  recorded**.
- Cloudflare answered 503 after ~45 quick calls → keep ≥1 s between request starts; writes run one at
  a time.
- Product creation may go through LKPP curation; a successful create may mean "submitted", not "live".
  The tool must report the status the site returns, not assume.

## 4. Approaches considered (penyedia request transport)

| | Approach | Trade-off |
|---|---|---|
| **A (chosen)** | Python drives the skill's Chrome; API calls run as `fetch()` inside a penyedia page via CDP `Runtime.evaluate` | Browser handles cookies, token refresh, Cloudflare clearance with a genuine fingerprint; no tokens leave Chrome's profile. Chrome must run (headless is fine, auto-started). |
| B | Copy cookies/token out, call the API from Python | Works with Chrome closed, but stores tokens in a file, re-implements refresh, and Cloudflare clearance is often bound to the browser that earned it → risk of 403s on writes. |
| C | A with B as fallback | Most coverage, double the code and testing for little gain. |

Owner picked **A**. For the session profile, reading the normal Chrome profile was ruled out (Chrome 136
policy, encrypted cookies), so a skill-owned profile is the only option.

## 5. Design

Delivered as two plans. **Plan A** (parts 5.1–5.5) is fully specified here. **Plan B** (5.6) fixes the
interface and safety model now; its request details are filled in from the first recording.

### 5.1 Scrapling removal (Plan A)
- Remove `FetcherSession`/`StealthySession` code, `_scrapling_available`, `--impersonate`, `--http3`,
  `./install.sh scrapling`, and the Scrapling rows/sections in `SKILL.md`, `README.md`, the module docstring.
- `--engine` keeps `auto|stdlib` for compatibility (both mean stdlib). `--engine scrapling` /
  `INAPROC_ENGINE=scrapling` **warn on stderr and continue** with the built-in engine ("Scrapling support
  was removed in v1.1; using the built-in engine"). `--impersonate` and `--http3` stay accepted but
  hidden (`argparse.SUPPRESS`) and print the same kind of warning, so v1.0 scripts keep running.
  *(builder-check call)* `engine` command keeps reporting
  settings (engine, headers profile, interval, concurrency, proxy).
- Version → v1.1.0 after Plan A, v1.2.0 after Plan B.

### 5.2 Browser headers in the stdlib engine (Plan A)
- Replace the `urllib` POST path with a per-thread persistent `http.client.HTTPSConnection`
  (keep-alive; reconnect on `RemoteDisconnected`/`BrokenPipe`). Proxy via `set_tunnel` (CONNECT);
  proxy credentials → `Proxy-Authorization` on the tunnel.
- One `CHROME_MAJOR` constant drives both `user-agent` and `sec-ch-ua`, so they never disagree.
  `sec-ch-ua-platform` follows the OS the script runs on (macOS/Windows/Linux), and the UA string matches.
- Header order for the GraphQL POST (HTTP/1.1, Chrome casing):
  `Host, Connection, Content-Length, sec-ch-ua-platform, User-Agent, sec-ch-ua, Content-Type,
  sec-ch-ua-mobile, Accept, Origin, Sec-Fetch-Site: same-origin, Sec-Fetch-Mode: cors,
  Sec-Fetch-Dest: empty, Referer, Accept-Encoding: gzip, deflate, Accept-Language:
  id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7, Cookie` (Cookie only when the jar has some).
- Cookies: keep a per-client cookie store (Set-Cookie parsed with `http.cookies.SimpleCookie`, sent back
  for matching host). Response bodies: decode gzip/deflate by `Content-Encoding`.
- Existing retry/backoff, rate limiter and error messages stay as they are.

### 5.3 Chrome control module (Plan A)
New file `scripts/inaproc_chrome.py`, stdlib only, imported lazily by `discover` and `penyedia`.

- **Find Chrome:** `--chrome-path` / `INAPROC_CHROME`, else known paths per OS (Chrome, Chromium, Edge,
  Brave; macOS `.app` bundles, Linux `which`, Windows Program Files/LocalAppData). Clear error if none.
- **Profiles:** two, both under `~/.inaproc/` (`INAPROC_HOME` overrides), created with mode 700:
  `chrome-penyedia` holds the login and is used only by `penyedia` commands; `chrome-public` is a
  never-logged-in profile used only by `discover`, so captured operations stay public-only and
  `discover` never restarts the Chrome that holds the login. The module takes a profile name.
  Profiles are never copied, uploaded or printed. *(builder-check call)*
- **Launch / reuse:** if `<profile dir>/DevToolsActivePort` exists and `http://127.0.0.1:<port>/json/version`
  answers, reuse that Chrome. Otherwise launch with `--user-data-dir=<profile>
  --remote-debugging-port=0 --remote-debugging-address=127.0.0.1 --no-first-run
  --no-default-browser-check` (+ `--headless=new` unless headed), then read the port from
  `DevToolsActivePort`. If a running instance is headless but a headed window is needed (login,
  record), close it and relaunch headed. Chrome launched by a command is closed at the end of that
  command unless `--keep-open`.
- **Session persistence check (first Plan A step for 5.5):** after `penyedia login`, close Chrome,
  relaunch headless, run `status`. If the login survives (persistent cookies), keep the close-after-
  command behaviour. If it doesn't (session-only cookies), switch `penyedia` to a background headless
  Chrome that later commands reuse and that exits by itself after 30 minutes idle; `status` then reports
  "signed out because Chrome was closed" when no instance is running. *(builder-check call)*
- **WebSocket client:** RFC 6455 over a plain socket to 127.0.0.1 only (refuse any other host):
  handshake with key check, masked client frames, 7/16/64-bit lengths, continuation frames, ping→pong,
  close. ~150 lines.
- **CDP client:** connects to the browser endpoint; `Target.attachToTarget(flatten=True)` for tabs;
  `send(method, params, session)` with ids and timeout; event subscription by method name; a reader
  thread dispatching responses and events.
- **Helpers:** `open_tab(url)`, `wait_network_idle(session, quiet_ms=500, timeout)`,
  `block_urls(session, patterns)` (`Network.setBlockedURLs`), `page_fetch(session, url, method,
  headers, body)` → `{status, headers, text}` via `Runtime.evaluate(awaitPromise=True,
  returnByValue=True)` with `credentials: "include"`.

### 5.4 `discover` on CDP (Plan A)
- Same CLI, same output table and `--update-queries` file (`scripts/queries.discovered.json`).
  Flags: `urls`, `--keyword`, `--headed`, `--wait`, `--update-queries`, `--chrome-path`, `--proxy`
  (passed to Chrome as `--proxy-server`).
- Opens each URL (still restricted to katalog.inaproc.id) in the skill profile, `Network.enable`,
  blocks the tracker list (google-analytics, googletagmanager, clarity, facebook.net, doubleclick),
  records `Network.requestWillBeSent` POSTs to `/graphql` (calls `Network.getRequestPostData` when the
  body is not inline), pairs with `Network.responseReceived` for status, waits for network idle plus
  `--wait` seconds.

### 5.5 Login and recorder (Plan A)
New command group `penyedia` (works only where a local Chrome exists; errors clearly otherwise).

- `penyedia login [--timeout 600]` — opens a **headed** window on `https://penyedia.inaproc.id/products`.
  The owner logs in themselves. The command polls every 3 s, never reading input fields or keystrokes.
  **Signed-in check:** a second hidden tab on `https://daftar-akun.inaproc.id/` (same origin as the
  endpoint, so no CORS) runs `fetch('/api/auth/session')` and **filters inside the page**, returning
  only `{signedIn, name, company, expires}` — the raw body (which may hold an access token) never
  reaches Python. Field names assume NextAuth's shape (`user.name`, `expires`); the builder confirms them
  with one by-hand call while the owner is logged in (allowed: a single request, not a sweep). Fallback
  if the endpoint doesn't answer: signed in = the penyedia tab has left the login/auth pages and
  `/products` loads. Then prints name/company and expiry if available. *(builder-check call)*
- `penyedia status` — headless; reports signed in or not, name, expiry. Exit code 0 / 3 (not signed in).
- `penyedia logout` — clears cookies and storage for `*.inaproc.id` in the profile
  (`Storage.clearDataForOrigin` per origin) and confirms.
- `penyedia record [--duration MIN]` — headed window on penyedia. Records **every request the
  site's tabs send except the tracker block list** — including third-party hosts such as presigned
  upload URLs and SSO hops — and follows popups/new tabs (`Target.setAutoAttach`, flatten). For each
  (GraphQL, REST, file uploads): method,
  URL, request headers, request body (multipart → part names, filenames, sizes, content types; file
  bytes not stored), status, response headers, response body (JSON in full up to 1 MB, else truncated).
  Stops when the window is closed, the tab navigates away from inaproc for good, or `--duration` ends.
  **Redaction before writing:** `Authorization`, `Cookie`, `Set-Cookie`, and any JSON field or query
  parameter whose name matches `token|secret|password|session|csrf|signature|credential|key`
  (covers presigned-URL signatures) → `"<redacted>"`. Writes
  `~/.inaproc/recordings/penyedia_<timestamp>.json` (mode 600) and prints its path and a summary table
  (operation/endpoint, method, count, status).

**Where penyedia state lives** *(builder-check call)*: recordings, plan files and the write log are
private working state, not exports, so they live in one fixed place — `~/.inaproc/recordings/` and
`~/.inaproc/penyedia/` (dirs mode 700, files 600) — keeping the audit log in a single file. `--save`
/ `--out-dir` still work as today for exporting listings (`penyedia products --save xlsx --out-dir .`),
and the SKILL.md rule to ask the user for format and location applies to those exports.

### 5.6 Seller product commands (Plan B — interface now, details after recording)
Transport: `page_fetch` from a penyedia tab in the skill's Chrome (headless, auto-started). Auth is
whatever the recording shows the site doing (cookies automatically; if a bearer token is needed, it is
read inside the page from the site's own session endpoint for each call and never returned to Python).
Concurrency 1, ≥1 s between requests, same backoff on 429/5xx.

Commands:
- `penyedia products [--status S] [--search Q] [--page N] [--per-page N]` — my products (id, name,
  SKU/variant count, price, stock, status, catalogue link). Supports `--json/--save`.
- `penyedia product get ID` — full editable detail.
- `penyedia product create SPEC.json` — dry run: validates the spec against the category's required
  fields (fetched live), resolves images (local file paths → uploaded at send time), prints the payload,
  writes a plan file.
- `penyedia product update ID [--price X] [--stock N] [--set field=value …]` — dry run: fetches current
  product, prints a field-level before/after diff, writes a plan file.
- `penyedia bulk-update FILE.csv|xlsx` — one row per product/variant (columns: `id`, plus any of the
  editable fields). Dry run diffs every row; rows with no change are skipped; invalid rows are listed
  and block the plan.
- `penyedia product delete ID [ID …]` — dry run: fetches each product, prints what will be removed
  (name, SKU, price, status, link), writes a plan file. Only products owned by the signed-in account.
  *(added 2026-11-23 at the owner's request)*
- `penyedia apply PLAN.json` — executes the plan exactly.

Safety model:
1. Every create/update/bulk-update/delete is a dry run. It writes
   `~/.inaproc/penyedia/plan_<timestamp>.json` containing each write's exact request payload and,
   for updates, a hash of the product's current state.
2. Only `penyedia apply PLAN` sends. Before each update it re-fetches the product; if the hash differs
   from the plan, that write is skipped and reported as "changed since dry run".
3. `SKILL.md` rule for agents: show the user the dry-run output and get an explicit yes for that plan
   before running `apply`. Never run `apply` on a plan the user hasn't seen.
4. Every sent write appends one JSON line to `~/.inaproc/penyedia/writes.log.jsonl`: time, plan
   file, operation, product id, payload, HTTP status, site response (redacted as in 5.5), result.
5. The command group exposes only product create/update/delete operations; the client refuses any other
   mutation name/endpoint even via `raw`-style use.
6. `apply` stops at the first failed write unless `--continue-on-error`; it reports done/failed/skipped.

## 6. Testing

- New `tests/` using `unittest` (stdlib), runnable with `python3 -m unittest`.
- Offline unit tests: header builder (order, UA/sec-ch-ua agreement, platform), cookie store,
  gzip decoding, WebSocket framing against a local stdlib echo server, CDP message dispatch with a fake
  socket, redaction, plan/diff/hash logic with a fake transport.
- Offline integration (skipped when no Chrome found): launch Chrome headless on a skill profile in a
  temp `INAPROC_HOME`, `discover` against a local `http.server` page that POSTs to `/graphql`
  (the URL-domain check is bypassed only by a test hook), `page_fetch` against the same server.
- Live checks stay light (rate limit): one `ppn` call through the new engine, one `discover` on the
  home page. Penyedia live checks are done by hand with the owner.

## 7. Left to the builder
- Internal structure of `inaproc_chrome.py` and class names; reader-thread vs. poll design.
- Exact Chrome path list per OS; exact `CHROME_MAJOR` value (current stable at build time).
- Network-idle heuristic details; table formatting of summaries.
- Plan B request shapes, field names, required-field lookup, image upload flow — from the recording.

## 8. Order of work
1. Plan A: 5.1 → 5.2 → 5.3 → 5.4 → 5.5, tests alongside, docs updated, reinstall (`./install.sh claude`,
   `codex`, `zip`).
2. Owner runs `penyedia login`, then `penyedia record` while adding a test/draft product and editing one.
3. Plan B written from the recording; built.
4. End-to-end acceptance with the owner signed in: `penyedia products` lists the account's products;
   `product create` → `apply` adds a clearly-named test product; `product delete` → `apply` removes
   it; `products` confirms both. Each `apply` is run only after the owner approves that plan.
