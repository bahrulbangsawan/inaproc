# Chrome engine + penyedia product commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove Scrapling, give the stdlib engine Chrome headers, drive a skill-owned Chrome over CDP for `discover`, and add `penyedia` login/record/list/get/create/update/delete/apply commands with dry-run plans.

**Architecture:** `inaproc.py` keeps the public CLI. New stdlib modules beside it: `inaproc_chrome.py` (find/launch Chrome, WebSocket, CDP client, page_fetch) and `inaproc_penyedia.py` (session check, recorder, seller GraphQL client, plans, write log). `inaproc.py` imports them lazily.

**Tech Stack:** Python 3.8+ standard library only; Chrome/Chromium/Edge/Brave via the DevTools Protocol; `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-23-chrome-engine-and-penyedia-design.md`

## Global Constraints
- Standard library only, Python ≥ 3.8; macOS, Linux, Windows.
- ≥ 1 s between request starts; seller writes concurrency 1; backoff 5 s / 20 s on 429/502/503/504.
- Profiles: `~/.inaproc/chrome-penyedia` (login), `~/.inaproc/chrome-public` (discover); `INAPROC_HOME` overrides `~/.inaproc`; dirs mode 700, files 600.
- Recordings `~/.inaproc/recordings/penyedia_<ts>.json`; plans `~/.inaproc/penyedia/plan_<ts>.json`; log `~/.inaproc/penyedia/writes.log.jsonl`.
- Redact names matching `token|secret|password|session|csrf|signature|credential|key` and headers `Authorization`, `Cookie`, `Set-Cookie` → `"<redacted>"`.
- WebSocket/CDP connects to 127.0.0.1 only.
- Seller endpoint `https://penyedia.inaproc.id/graphql`, cookie auth (`credentials: "include"`), called only via `page_fetch` from a penyedia.inaproc.id tab.
- Allowed seller operations: queries `getListProduct`, `getProductById`, `checkProductNameAvailability`, `getAllProductCategoryListSeller`, `requestUploadSignedUrl`; mutations `createProduct`, `createProductDraft`, `updateProduct`, `updateProductDraft`, `deleteProduct`, `updateProductActiveStatus`. Anything else is refused.
- No write without a plan file; only `penyedia apply PLAN` sends.
- Version v1.1.0 after Task 5, v1.2.0 after Task 9.

## Review Focus
- Chrome already running on the profile (user left the login window open) → reuse it, don't launch a second one (Task 3 test `test_reuse_running_instance`).
- Session expired between dry run and apply → apply stops with "not signed in, run `penyedia login`", sends nothing (Task 8 test `test_apply_requires_session`).
- GraphQL answers HTTP 200 with a `GenericError` union member → treated as failure, logged, apply stops (Task 7 test `test_generic_error_is_failure`).
- Plan file edited by hand or for another account → refused when `sellerId`/account differs (Task 8 test `test_plan_account_mismatch`).
- Product changed on the site after the dry run → that write skipped as "changed since dry run" (Task 8 test `test_hash_changed_skips`).

---

### Task 1: Remove Scrapling, compatibility warnings

**Files:** Modify `skills/inaproc/scripts/inaproc.py` (docstring, `_scrapling_available`, `Client`, `cmd_engine`, parser), `install.sh`, `install.ps1`, `skills/inaproc/SKILL.md`, `README.md`. Test: `tests/test_engine.py`.

**Interfaces:** Produces `Client(proxy=None, min_interval=1.0, concurrency=1, timeout=30)` (engine args gone), `compat_warnings(args) -> list[str]`.

- [ ] Test `test_scrapling_engine_warns`: `main(["ops","--engine","scrapling"])` returns 0 and stderr contains `Scrapling support was removed in v1.1`. `test_impersonate_flag_accepted`: `["ops","--impersonate","chrome","--http3"]` returns 0 with a warning. `test_engine_choices`: parser accepts `auto`, `stdlib`.
- [ ] Run `python3 -m unittest tests.test_engine -v` → FAIL.
- [ ] Implement: delete Scrapling code paths; `--impersonate`/`--http3` kept with `help=argparse.SUPPRESS`; warnings printed once to stderr; remove `install.sh scrapling` target and docs rows (README "Network engine and Scrapling" section rewritten to the new header engine + Chrome note).
- [ ] Run tests → PASS. Commit `Remove Scrapling engine; keep old flags as warnings`.

### Task 2: Chrome-like headers over http.client

**Files:** Modify `inaproc.py` (`UA`, `BASE_HEADERS` → `chrome_headers()`, `Client._post_stdlib` → `Client._post`). Test `tests/test_engine.py`.

**Interfaces:** Produces `CHROME_MAJOR = 142`, `chrome_platform() -> str` (`"macOS"|"Windows"|"Linux"`), `user_agent() -> str`, `chrome_headers(body_len: int, cookie: str|None) -> list[tuple[str,str]]`, `CookieStore.update(set_cookie_headers: list[str])`, `CookieStore.header() -> str|None`.

- [ ] Tests: `test_header_order` asserts names in exactly this order: `Host, Connection, Content-Length, sec-ch-ua-platform, User-Agent, sec-ch-ua, Content-Type, sec-ch-ua-mobile, Accept, Origin, Sec-Fetch-Site, Sec-Fetch-Mode, Sec-Fetch-Dest, Referer, Accept-Encoding, Accept-Language` (+ `Cookie` last only when given); `test_ua_matches_sec_ch_ua` (`f"Chrome/{CHROME_MAJOR}.0.0.0"` in UA and `f'v="{CHROME_MAJOR}"'` in sec-ch-ua); values `Accept-Encoding: gzip, deflate`, `Accept-Language: id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7`, `Sec-Fetch-Site: same-origin`; `test_cookie_store_roundtrip`; `test_gzip_decoded` against a local `http.server` over plain HTTP via an injectable `(scheme, host, port)` on `Client`.
- [ ] Run → FAIL.
- [ ] Implement `_post(body) -> (status, text)` with a per-thread `HTTPSConnection` (`putrequest(..., skip_host=True, skip_accept_encoding=True)` then `putheader` in order), reconnect once on `RemoteDisconnected/BrokenPipeError/ConnectionResetError`; proxy via `set_tunnel`; decode `gzip`/`deflate`.
- [ ] Run tests → PASS. Live check (one call): `python3 skills/inaproc/scripts/inaproc.py ppn` prints rates. Commit `Chrome-ordered headers over http.client`.

### Task 3: inaproc_chrome — find, launch, WebSocket, CDP

**Files:** Create `skills/inaproc/scripts/inaproc_chrome.py`. Tests `tests/test_chrome.py`.

**Interfaces (produces):**
- `inaproc_home() -> pathlib.Path`; `profile_dir(name: str) -> Path` (creates, mode 700).
- `find_chrome(explicit: str|None = None) -> str` raises `ChromeError`.
- `class WebSocket: connect(url) ; send_text(s) ; recv_text(timeout) -> str ; close()` (loopback only, raises `ChromeError` for other hosts).
- `class Browser` — `Browser.start(profile: str, headed: bool, chrome_path=None, proxy=None, keep_open=False) -> Browser` (reuses a live `DevToolsActivePort`; relaunches headed if needed); `.new_tab(url) -> Tab`; `.tabs(url_prefix) -> list[Tab]`; `.close()`; context manager.
- `class Tab` — `.send(method, params=None, timeout=30) -> dict`; `.on(event, callback)`; `.wait_network_idle(quiet_ms=500, timeout=60)`; `.block_urls(patterns)`; `.evaluate(js, await_promise=True)`; `.page_fetch(url, method="POST", headers=None, body=None) -> dict(status, headers, text)`; `.url() -> str`; `.closed -> bool`.

- [ ] Tests (offline): `test_ws_echo` against a stdlib threaded echo server (frames 10 B, 70 KB, 70 000 B continuation); `test_ws_refuses_remote_host`; `test_cdp_dispatch` with a fake WebSocket: responses matched by id, events routed; `test_find_chrome_env` (`INAPROC_CHROME` honoured; missing path → `ChromeError`).
- [ ] Tests (skip if `find_chrome()` raises): `test_launch_and_eval` (`evaluate("1+1") == 2`, headless, temp `INAPROC_HOME`); `test_reuse_running_instance` (second `Browser.start` with `keep_open` reuses the same port, one Chrome process); `test_page_fetch_local` (local http.server echo JSON).
- [ ] Run → FAIL. Implement. Launch flags: `--user-data-dir=<dir> --remote-debugging-port=0 --remote-debugging-address=127.0.0.1 --no-first-run --no-default-browser-check` (+ `--headless=new`, `--proxy-server=`). Port from `<dir>/DevToolsActivePort` (poll 15 s). Browser-level socket from `/json/version`; tabs via `Target.createTarget` + `Target.attachToTarget(flatten=True)`; one reader thread.
- [ ] Run → PASS. Commit `Add stdlib Chrome DevTools client`.

### Task 4: discover on CDP

**Files:** Modify `inaproc.py` (`cmd_discover`, parser). Test `tests/test_discover.py`.

**Interfaces:** Consumes `Browser`, `Tab`. Produces `capture_graphql(tab, urls, wait_s) -> list[dict(operationName,status,page,query,exampleVariables)]`; `TRACKERS = ["*google-analytics.com*","*googletagmanager.com*","*clarity.ms*","*facebook.net*","*doubleclick.net*"]`.

- [ ] Test (skip without Chrome): local server page whose JS POSTs `{"operationName":"localOp","query":"query localOp{x}","variables":{}}` to `/graphql`; `capture_graphql` returns one op `localOp` with status 200. `test_discover_rejects_other_domains` (`main(["discover","https://example.com"])` → 1).
- [ ] Run → FAIL. Implement with profile `chrome-public`; `Network.enable`, `requestWillBeSent` (POST + `/graphql`), `getRequestPostData` fallback, `responseReceived` for status; parser drops Scrapling wording.
- [ ] Run → PASS. Live check (light): `discover --wait 5` lists ≥1 known op. Commit `discover via Chrome DevTools Protocol`.

### Task 5: penyedia login / status / logout / record

**Files:** Create `skills/inaproc/scripts/inaproc_penyedia.py`; modify `inaproc.py` parser (subcommand `penyedia` with nested subparsers). Tests `tests/test_penyedia.py`.

**Interfaces:** Produces `PENYEDIA = "https://penyedia.inaproc.id"`; `SESSION_JS` (runs in a `https://daftar-akun.inaproc.id/` tab, returns only `{signedIn,name,company,expires}`); `session_info(browser) -> dict`; `redact(obj) -> obj`; `redact_headers(dict) -> dict`; `Recorder(browser).run(duration_s|None) -> Path`; exit code 3 when not signed in.

- [ ] Tests: `test_redact_nested` (`{"accessToken":"x","a":{"X-Amz-Signature":"y","name":"ok"}}` → token and signature redacted, `name` kept); `test_redact_headers` (Authorization/Cookie/Set-Cookie); `test_redact_query_params` (`?token=a&page=2` → `token=<redacted>&page=2`); `test_recording_file_mode` (written file mode `0o600`); `test_status_exit_code_not_signed_in` with a fake `session_info` → `main(["penyedia","status"]) == 3`.
- [ ] Run → FAIL. Implement. `login`: headed `chrome-penyedia`, opens `/products`, polls `session_info` every 3 s until signedIn (timeout `--timeout`, default 600). Fallback check: tab URL on penyedia and not under `/auth`. `record`: `Target.setAutoAttach(autoAttach=True, flatten=True, waitForDebuggerOnStart=False)`, all requests except `TRACKERS`, response bodies via `Network.getResponseBody` (≤1 MB JSON), multipart summarised; stops on window close or `--duration`. `logout`: `Storage.clearDataForOrigin` for penyedia, daftar-akun, login-akun, akun, katalog origins.
- [ ] Run → PASS. Session-persistence check with the owner (spec 5.3): login, close, `status`. If signed out after relaunch, switch penyedia commands to keep a background Chrome (`keep_open=True`, idle exit 30 min via a watchdog flag file). Commit `penyedia login, status, logout, record`; bump `VERSION = "1.1.0"`; reinstall.

### Task 6: Owner recording (manual, gate for Tasks 7–9)

- [ ] Owner runs `penyedia login`, then `penyedia record`, adds one test product by hand (name starting `TEST HAPUS`, with one image), saves as draft and/or submits, edits its price, deletes it.
- [ ] From the recording, write `skills/inaproc/references/penyedia-api.md`: exact `CreateProductInput`/`CreateProductDraftInput` used, `getListProduct` filter, image upload sequence (`requestUploadSignedUrl` input incl. `recipientKey`, PUT target, what token goes into the product input), delete response, curation status after create. Save operation texts to `skills/inaproc/scripts/penyedia_queries.json` (only the allowed operations).

### Task 7: Seller client + read commands

**Files:** `inaproc_penyedia.py`, `inaproc.py` parser. Tests `tests/test_penyedia.py`.

**Interfaces:** Produces `class SellerClient(tab, limiter)`: `.gql(op, variables) -> dict` (refuses ops not in the allowed list → `InaprocError`; GenericError member → `InaprocError(code, message, reqId)`); `.list_products(page, per_page, filter) -> dict`; `.get_product(id) -> dict`; `.signed_in() -> bool`. Commands `penyedia products [--status] [--search] [--page] [--per-page]` (+ `--json/--save`), `penyedia product get ID`.

- [ ] Tests with a fake tab: `test_refuses_unlisted_op` (`gql("cartCheckoutWithoutNegotiation", {})` raises); `test_generic_error_is_failure`; `test_list_products_rows` (rows: id, name, variants, price, stock, status, link `https://katalog.inaproc.id/<username>/<slug>`).
- [ ] Run → FAIL → implement → PASS. Live: `penyedia products` lists the owner's products. Commit.

### Task 8: Plans, diff, apply, write log

**Files:** `inaproc_penyedia.py`, `inaproc.py`. Tests `tests/test_plans.py`.

**Interfaces:** Produces `state_hash(product: dict) -> str` (sha256 of canonical JSON of editable fields); `make_plan(kind, account, writes: list[dict]) -> Path` (each write `{op, variables, productId?, beforeHash?, summary}`; plan has `account`, `createdAt`, `version: 1`); `diff(before: dict, after: dict) -> list[(path, old, new)]`; `apply_plan(client, path, continue_on_error=False) -> dict(done, failed, skipped)`; log line `{time, plan, op, productId, variables(redacted), status, response(redacted), result}`.

- [ ] Tests (fake client): `test_plan_written_mode_600`; `test_apply_requires_session`; `test_plan_account_mismatch`; `test_hash_changed_skips`; `test_stop_on_first_failure` / `--continue-on-error`; `test_log_appends_one_line_per_write`.
- [ ] Run → FAIL → implement → PASS. Commit.

### Task 9: create / update / bulk-update / delete commands

**Files:** `inaproc_penyedia.py`, `inaproc.py`, `SKILL.md`, `README.md`, `references/penyedia-api.md`. Tests `tests/test_plans.py`.

**Interfaces:** Consumes Tasks 7–8. Commands: `penyedia product create SPEC.json [--draft]` (spec = the input shape documented in Task 6; local image paths resolved at apply time via the recorded upload flow); `penyedia product create --from ID --name NAME [--draft]` (clone an own product: `get_product` → input transform per `penyedia-api.md`); `penyedia product update ID [--price X] [--stock N] [--set path=value …]`; `penyedia bulk-update FILE.csv|xlsx` (columns `id` + `price`/`stock`/`name`/…; uses `read_xlsx` for xlsx); `penyedia product delete ID [ID …]`; `penyedia apply PLAN [--continue-on-error]`.

- [ ] Tests (fake client): `test_update_diff_lists_changed_fields_only`; `test_bulk_skips_unchanged_and_blocks_invalid`; `test_delete_plan_lists_name_and_link`; `test_create_from_clone_sets_new_name`.
- [ ] Run → FAIL → implement → PASS.
- [ ] SKILL.md: penyedia section with the agent rule "show the dry run and get an explicit yes for that plan before `apply`"; works only in Claude Code/Codex with local Chrome. Bump `VERSION = "1.2.0"`; reinstall claude/codex/zip. Commit.

### Task 10: End-to-end acceptance with the owner signed in
- [ ] `penyedia status` → signed in.
- [ ] `penyedia products` → lists the account's products.
- [ ] `penyedia product create … --name "TEST HAPUS <date>"` → show plan → owner approves → `apply` → `products --search "TEST HAPUS"` shows it (status as returned, e.g. draft/curation).
- [ ] `penyedia product delete <id>` → show plan → owner approves → `apply` → `products --search "TEST HAPUS"` empty.
- [ ] `python3 -m unittest discover tests -v` all pass; write log has 2 lines. Commit.
