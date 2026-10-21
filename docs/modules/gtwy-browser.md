---
covers: ["src/services/utils/built_in_tools/browser/**", "src/routes/v2/browserRouter.py", "src/db_services/browserCookieService.py"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Gtwy_Browser

`Gtwy_Browser` is a built-in tool that lets the model drive a real Chromium running in a self-hosted [Steel](https://github.com/steel-dev/steel-browser) container. The model can navigate, read an accessibility snapshot with element refs, click, type, press keys, scroll, go back, and hand the tab to the user when a login, CAPTCHA or OTP is needed. The tool loop in [tool-calling](tool-calling.md) calls it as `call_gtwy_browser`. [agent-configuration](agent-configuration.md) registers it when `"Gtwy_Browser"` is in the agent's or the request's `built_in_tools` and a Steel host is configured.

| File | Role |
|---|---|
| `src/services/utils/built_in_tools/browser/tool.py` | Entry point `call_gtwy_browser`: URL check, tab setup under a 25 s budget, handoff gate, action dispatch, result shaping. It never raises. |
| `src/services/utils/built_in_tools/browser/tabs.py` | Picks the host and tab for a conversation (`open_or_reuse_tab`), places new conversations, evicts idle tabs, forgets tabs of a replaced Chrome (`reconcile_registry`), `release_tab`, `mark_handoff`. |
| `src/services/utils/built_in_tools/browser/session_store.py` | Redis state: one registry per Steel host plus per-conversation state, the registry lock, `BrowserBusy`, idle limits, `MAX_TABS_PER_HOST`. |
| `src/services/utils/built_in_tools/browser/connection.py` | One Playwright CDP connection per host per pod (`get_browser`). Raw-CDP tab create/close, cookie export/import, connect retry policy. |
| `src/services/utils/built_in_tools/browser/steel_client.py` | Steel REST client (`current_session`, `create_session`), the host pool (`hosts`), live-view URLs (`live_view_url`, `permanent_live_url`). |
| `src/services/utils/built_in_tools/browser/actions.py` | One coroutine per action. Every mutating action returns a fresh snapshot. Login-page heuristic `looks_like_login_page`. |
| `src/services/utils/built_in_tools/browser/snapshot.py` | Pure parser: Playwright aria snapshot to text with `[eN]` refs plus a ref map (`parse_aria_snapshot`, `truncate`). |
| `src/services/utils/built_in_tools/browser/security.py` | SSRF guard `ensure_url_allowed` and `wrap_untrusted`. |
| `src/services/utils/built_in_tools/browser/cookies.py` | Remembered logins: `save_jar` / `restore_jar` (encrypted, MongoDB only). |
| `src/services/utils/built_in_tools/browser/reaper.py` | Background loop on every pod that closes idle tabs and forgets the tabs of a replaced Chrome. |
| `src/services/utils/built_in_tools/browser/schema.py` | The flat tool schema the model sees (`build_browser_tool_schema`, `BROWSER_ACTIONS`, `DESCRIPTION`). |
| `src/routes/v2/browserRouter.py` | `GET /browser/live/{token}`: the permanent live-view link. It resolves the conversation's current tab and redirects. |
| `src/db_services/browserCookieService.py` | The `gtwy_browser_cookies` collection: `ensure_indexes`, `get_browser_cookies`, `upsert_browser_cookies`. |

## How it works

```
model ──tool call──▶ gtwy (call_gtwy_browser) ──REST──▶ Steel /v1/sessions        (which Chrome is running)
                          │                    ──CDP (Playwright)──▶ Chrome in Steel (one connection per host per pod)
                          ├── Redis: registry per host (tabs by conversation) + per-conversation ref map/handoff
                          └── Mongo: gtwy_browser_cookies (encrypted cookie jar per conversation)

api-1 Chrome ── tab (own cookie jar) ── conversation A   logged in as user A
             ├─ tab (own cookie jar) ── conversation B   logged in as user B
             └─ tab (own cookie jar) ── conversation C   (host full at 3)
api-2 Chrome ── tab (own cookie jar) ── conversation D
```

### One call, end to end

1. `call_gtwy_browser(args, ctx)` wraps `_run` in a 60 s `asyncio.wait_for` and turns every exception into an error result. `ctx` comes from `_browser_ctx` in `src/services/commonServices/baseService/utils.py`.
2. `_run` returns an error if no Steel host is configured or if `action` is not in `BROWSER_ACTIONS`. For `navigate` it runs `ensure_url_allowed` before it claims a tab, so a blocked URL never opens a browser.
3. The conversation key is `thread_key(org_id, thread_id, sub_thread_id)` = `"<org>:<thread>:<sub_thread or thread>"`.
4. `open_or_reuse_tab` runs with a 25 s budget (`SETUP_TIMEOUT_SECONDS`):
   - It reads every host's registry and finds the conversation's home host (`find_tab_host`).
   - It tries the home host first: reuse its tab, or open a new tab there if the old one is gone and there is room. Then it tries the other hosts, fewest open tabs first (`_placement_order`).
   - Each attempt (`_try_host`) holds that host's Redis registry lock. It asks Steel which session is running (`_ensure_session_and_browser`), forgets tabs if Chrome was replaced (`reconcile_registry`), and connects over CDP (`get_browser`).
   - A full host first closes idle tabs of other conversations, oldest first (`evictable_tabs`), saving their cookies. A new tab is a raw-CDP context plus target (`create_isolated_tab`), downloads are denied, and the conversation's saved cookies go back in (`restore_jar`).
   - If every host is full it raises `BrowserBusy` with a retry time. If every host failed to connect it raises `BrowserConnectionError`.
5. `live_url` is `permanent_live_url(...)` (a signed gtwy link) or, without `GTWY_PUBLIC_URL`/`SecretKey`, the direct Steel link `<debugUrl>?pageId=<target_id>`.
6. Handoff gate: see [Login handoff](#login-handoff).
7. `_dispatch` runs the action from `actions.py`. A new snapshot replaces `ref_map` in the per-conversation state and increments `snapshot_seq`.
8. If the action produced a snapshot and `looks_like_login_page` is true, the handoff starts by itself (`login_required: true`, `instructions`, `browser_handoff` SSE).
9. The thread state is saved. `snapshot` and `title` are wrapped in `<<<UNTRUSTED_WEB_CONTENT>>>` markers. Every success result carries `live_url`.

The browser calls in one model turn run one after another (`_run_tool_tasks`), while other tools run in parallel, because they drive the same tab.

### Snapshot loop and refs

`snapshot_page` takes `page.locator("body").aria_snapshot()`. `parse_aria_snapshot` appends `[eN]` to every node whose role is in `INTERACTIVE_ROLES`, and records `{"eN": {"role", "name", "nth"}}`. `nth` counts identical (role, name) pairs, so `resolve_ref` finds the element again with `page.get_by_role(role, name=..., exact=True).nth(nth)`. `truncate` cuts the text at 15,000 chars and drops refs past the cut. A ref missing from the latest map raises `StaleRefError` ("stale ref …; call snapshot").

### One tab per conversation, on a pool of hosts

- Steel OSS runs one Chrome per container. gtwy gives each conversation its own tab in it. Each tab is created with raw CDP (`Target.createBrowserContext` with `disposeOnDetach: False`, then `Target.createTarget`), so it has its own cookie jar. Two conversations can be signed in to the same site as different people.
- Tabs are recorded per host by Chrome target id in the host's registry, so any gtwy pod can find the tab again. Returning to a conversation within its idle window reuses the same tab, still logged in.
- `STEEL_API_URLS` lists the pool (`STEEL_API_URL` works for one host). A conversation stays on its home host, so its live tab stays with it. A new conversation goes to the host with the fewest tabs. A host takes at most `STEEL_MAX_TABS_PER_HOST` conversations (default 3). An unreachable host is skipped.

### gtwy does not own the Chrome

Steel OSS always keeps one session up. After any release it starts a default one by itself. Creating a session restarts Chrome, and so does releasing one, even with a stale or unknown id (verified against Steel). A restart kills every conversation's tab on that host and drops every live view. The CDP endpoint (`cdp_ws_url`, the API root over wss) is not tied to a session. So:

- Before every call gtwy asks `GET /v1/sessions` for the newest session that is not released (`pick_current_session`) and attaches to it.
- It calls `create_session` only when Steel reports no session at all. No code calls release.
- If the running session id differs from the registry's `steel_session_id`, someone else replaced Chrome (a person in the Steel UI, Steel itself, another client). `reconcile_registry` logs `Gtwy_Browser: Chrome on <host> was replaced (session …`, clears those conversations' state and starts an empty registry, so dead tabs are never handed out.
- A failed connect returns "the browser did not answer; … try the same action again" and drops only the Playwright driver (`reset_connection`). It never restarts Chrome.

### Login handoff

- The handoff starts by itself after any snapshotting action when the page looks like a sign-in page (`looks_like_login_page`): the URL path matches `LOGIN_PATH_RE`, the host is in `LOGIN_HOSTS`, or a visible password field exists. The model can also start it with `request_user_action`.
- `_begin_handoff` stores `handoff_active`, `handoff_message`, `handoff_message_id` (the current `message_id`) and `handoff_started_at` in the thread state. `mark_handoff` copies the flag onto the tab record, so the reaper waits 900 s instead of 300 s.
- While the handoff is active, every action except `navigate` and `request_user_action` is refused ("user is interacting with the browser…"). No snapshots means nothing the user types reaches the model or the logs.
- It ends when a call arrives with a different `message_id` (the user's next message; `chat_completion` in `src/routes/v2/modelRouter.py` generates a new one per request) or when the model navigates.
- `do_type` refuses password fields. The schema has no JavaScript action.

### Remembered logins

A tab's cookies are exported just before gtwy closes it (reaper, eviction to free a slot, "tab lost") and restored into the next tab the conversation opens. Export and import use CDP `Storage.getCookies` / `Storage.setCookies` with the jar's `browserContextId`, because Steel's own session-context endpoint cannot see these jars. `_trim` keeps only `SAFE_COOKIE_FIELDS`, since `Storage.setCookies` rejects Chrome's read-only fields. The blob is `Helper.encrypt(json)` from `src/services/utils/helper.py`. `is_enabled` turns saving off when `Encreaption_key` or `Secret_IV` is missing. MongoDB is the only store. A Mongo failure costs the user a fresh login and never fails the turn.

### Reaper

`index.py` starts `run_browser_reaper_loop` in the lifespan on every pod when `steel_client.is_configured()`, and it also schedules `ensure_indexes` for the cookie collection. Every 60 s, one pod per tick (Redis lock `gtwy_browser_reaper`, TTL 55 s) walks each host under its registry lock:

- reconcile with the running session (forget a replaced Chrome's tabs);
- for each tab idle past `tab_idle_limit`: save cookies, close the tab and its context, drop the record, clear the thread state;
- when no tabs are left, drop the Playwright driver and leave Chrome running.

If the reaper cannot connect, it leaves the tabs alone and tries again next tick.

## Contracts

**Enabling.** Add `"Gtwy_Browser"` (`inbuild_tools` in `src/configs/constant.py`) to the agent's `built_in_tools`, or send `"built_in_tools": ["Gtwy_Browser"]` in the request. `add_browser_tool` skips it with a warning when no Steel host is set. When it is on, `_resolve_maximum_iterations` in `src/services/utils/common_utils.py` sets the tool-loop limit to `max(configured or 3, 25)` (`GTWY_BROWSER_MIN_ITERATIONS`). An explicit 10 still becomes 25; only a larger value wins.

**Tool schema** (`build_browser_tool_schema`; flat because several providers reject nested unions):

| Param | Used by | Notes |
|---|---|---|
| `action` | all | `navigate`, `snapshot`, `click`, `type`, `press`, `scroll`, `back`, `request_user_action` (no screenshot action) |
| `url` | navigate | http(s) only; internal, private and Steel hosts are refused |
| `ref` | click, type | e.g. `e12`, from the latest snapshot |
| `text` | type | replaces the field value; password fields are refused |
| `key` | press, type | `Enter`, `Escape`, `Tab`, `ArrowDown`…; with `type` it is pressed after typing |
| `direction` | scroll | `up` or `down` (600 px) |
| `message` | request_user_action | what the user must do in the live view |

**Context in, result out:**

```python
ctx = {"org_id", "bridge_id", "thread_id", "sub_thread_id", "message_id",
       "tool_call_id",            # used as call_id on browser_handoff
       "streamer"}                # StreamingService only when stream=true, else None

ok  = {"response": {"url", "title", "snapshot", "truncated", "ref_count", "live_url",
                    # optional: "login_required": True, "instructions": "..."
                    # request_user_action: {"handoff": True, "live_url", "message", "instructions"}
                   },
       "metadata": {"type": "function", "browser": {"action", "url", "tab": "<last 6 of target id>", "host": "api-1"}},
       "status": 1}
err = {"response": {"error": "...", "live_url"?: "...", "retry_in_seconds"?: 5},
       "metadata": {"type": "function"}, "status": 0}
```

The tool loop sends the model `response` on success and `{"error": response}` on failure. The streamed `tool_result.content` and `response.data.tools_data.Gtwy_Browser` carry the same JSON. Read `live_url` there, not from the model's reply: the URL is long and a model may change or truncate it.

**SSE.** Besides `tool_call` / `tool_result`, the handoff emits `{"event": "browser_handoff", "live_url", "message", "call_id"}` through `emit_browser_handoff` in `src/services/commonServices/streaming_service.py`. The chat UI is expected to show `live_url` in an iframe (append `&clipboardBridge=true`, `allow="clipboard-read; clipboard-write"`) with a Done button that sends the next message. Without an iframe, show it as a "Log in" link.

**Live link.** `permanent_live_url` signs `{"o": org, "t": thread, "s": sub_thread, "exp": today 00:00 UTC + 30 days}` with `SecretKey` (HS256), giving `{GTWY_PUBLIC_URL}/browser/live/<jwt>`. The link is the same all day for a conversation. `live_view` decodes it (410 expired, 400 invalid), finds the conversation's tab, checks the host still runs the same session, and answers 302 to `<debugUrl>?pageId=<target_id>`. It returns 404 "No browser is open for this conversation" when there is no tab, and 503 when the registry has no `debug_url`. Route table: [api](../api.md).

**Redis keys.** Every key gets the `AIMIDDLEWARE_<ENVIRONMENT>_` prefix from `REDIS_PREFIX` in `src/services/cache_service.py`. `<host-slug>` is `host_slug(netloc)`. Details: [data-model](../data-model.md).

```
AIMIDDLEWARE_<ENV>_nd_gtwy_browser_registry_<host-slug>       TTL 5400 s, refreshed on save
  {"host", "steel_session_id", "debug_url", "created_at",
   "tabs": {"<org>:<thread>:<sub>": {"host", "target_id", "browser_context_id", "last_used_at",
                                      "handoff_active", "org_id", "bridge_id", "created_at"}}}
AIMIDDLEWARE_<ENV>_nd_gtwy_browser_thread_<org>:<thread>:<sub>  TTL 3600 s
  {"host", "target_id", "last_url", "snapshot_seq", "ref_map",
   "handoff_active", "handoff_message", "handoff_message_id", "handoff_started_at"}
AIMIDDLEWARE_<ENV>_lock_gtwy_browser_registry_<host-slug>     SET NX, TTL 30 s
AIMIDDLEWARE_<ENV>_lock_gtwy_browser_reaper                   SET NX, TTL 55 s
```

**Mongo `gtwy_browser_cookies`** (owned by this repo; gtwy-node does not read it). Indexes from `ensure_indexes`: unique `scope_key`, and TTL on `expires_at` with `expireAfterSeconds=0`. The deadline lives in the document, so changing the retention needs no index rebuild. `get_browser_cookies` also checks `expires_at`, because Mongo's TTL sweep runs only about once a minute.

```json
{ "scope_key": "<org>:<thread>:<sub_thread>", "cookies": "<Helper.encrypt(json list)>",
  "cookie_count": 6, "version": 1, "updated_at": "<utc>", "expires_at": "<utc + 30 d>",
  "org_id": "<if known>", "bridge_id": "<if known>" }
```

**Config.** `STEEL_API_URLS`, `STEEL_API_URL`, `STEEL_MAX_TABS_PER_HOST`, `GTWY_PUBLIC_URL`, `SecretKey`, `Encreaption_key`, `Secret_IV`: see [environment](../environment.md). `playwright` in `req.txt` is the CDP client only; no browser download is needed. Everything below is a code constant, because it describes how the browser behaves, not where it lives.

| Constant | File | Value | Why |
|---|---|---|---|
| `TOOL_TIMEOUT_SECONDS` | `tool.py` | 60 | Ceiling for one browser call |
| `SETUP_TIMEOUT_SECONDS` | `tool.py` | 25 | Tab setup on its own, so a slow start cannot eat the page's 30 s |
| `NAVIGATE_TIMEOUT_MS` / `ACTION_TIMEOUT_MS` / `SETTLE_TIMEOUT_MS` | `actions.py` | 30,000 / 10,000 / 3,000 | goto and back / click, fill, snapshot / load-state wait |
| `SNAPSHOT_MAX_CHARS` | `actions.py` | 15,000 | One huge page cannot eat the model's context |
| `CONNECT_TIMEOUT_MS` | `connection.py` | 7,000, one attempt | A hang does not clear by waiting; only a failure under `QUICK_FAILURE_SECONDS` (2 s) gets one retry |
| `FRESH_CONNECT_TIMEOUT_MS` × `FRESH_CONNECT_ATTEMPTS` | `connection.py` | 5,000 × 2, 1 s apart | A Chrome Steel just launched may refuse the first attempt |
| `TAB_APPEAR_TIMEOUT_SECONDS` | `connection.py` | 6 | Wait for a new target to show up in Playwright |
| `STEEL_HTTP_TIMEOUT` | `steel_client.py` | 10 s | A slower Steel REST answer means Steel is unwell |
| `IDLE_TIMEOUT_SECONDS` / `HANDOFF_TIMEOUT_SECONDS` | `session_store.py` | 300 / 900 | A user typing in the live view never reaches gtwy, so a tab mid-login looks idle |
| `MAX_TABS_PER_HOST` | `session_store.py` | `STEEL_MAX_TABS_PER_HOST`, default 3 | Memory on 1.5 GB hosts, see Why |
| `REGISTRY_LOCK_WAIT_SECONDS` | `session_store.py` | 5.0 | Wait by deadline, not by attempts: one SETNX was seen taking 700 ms |
| `REGISTRY_TTL_SECONDS` / `THREAD_STATE_TTL` | `session_store.py` | 5400 / 3600 | Safety net if the reaper dies |
| `COOKIE_TTL_SECONDS` | `cookies.py` | 30 days | How long a saved login lives |
| `LIVE_LINK_TTL_DAYS` | `steel_client.py` | 30 | The link sits in chat history and must outlive its tab |
| `REAPER_INTERVAL_SECONDS` | `reaper.py` | 60 | Reaper tick |
| `DNS_TIMEOUT_SECONDS` × `DNS_ATTEMPTS` | `security.py` | 5 × 2 | One retry on resolver hiccups, then fail closed |

A healthy Steel finishes the whole setup in about 1.5 s.

## Gotchas

> [!WARNING]
> Never call `browser.close()` or Playwright's `browser.new_context()`. Over CDP, `close()` tears down every conversation's context and login. Playwright-owned contexts die when the connection closes, which logs users out on every reconnect or pod restart. `_teardown_locked` only stops the Playwright driver.

> [!WARNING]
> Never release a Steel session, and never create one while `current_session` returns a session. Either call restarts Chrome on that host and kills every conversation's tab. Answering a connect failure with a restart once caused a chain: one conversation's hiccup killed the others' tabs, their "tab lost" cleanup released the session again, and so on. Steel's history showed bursts of three restarts within five seconds.

> [!WARNING]
> Logins are saved only when gtwy closes a tab (reaper, eviction, "tab lost"). If Chrome restarts or someone releases it in Steel's UI, logins made since the tab opened are lost. Deleting a cookie document while that conversation's tab is open does not forget the login either: the tab saves the same cookies back when it closes.

> [!WARNING]
> The Redis registry is the only record of which tabs exist. If a registry key expires or is deleted, its tabs stay open in Chrome, and nothing closes them until Chrome restarts. They do not count toward `MAX_TABS_PER_HOST`.

> [!WARNING]
> A failure to take the registry lock (5 s wait, or Redis down, because `acquire_lock` returns False on errors) raises `BrowserBusy(5, MAX_TABS_PER_HOST, 1)`. For the home host this error goes straight to the model as "all 3 browser tabs in the browser are in use…", even when the host has room.

> [!WARNING]
> `ensure_url_allowed` checks only `navigate`. Redirects, clicked links and `back` are not checked. The Steel hosts' network must not reach anything private.

> [!WARNING]
> Anyone who has the live link can watch and click inside that conversation's tab, with its logins, for up to 30 days, as long as a tab is open. There is no other auth on `GET /browser/live/{token}`. Treat the link like a credential.

> [!WARNING]
> `browser_handoff` is emitted only for `stream: true` requests (`_browser_ctx` passes no streamer otherwise). Non-stream callers must read `login_required` from `tools_data`. `tools_data.Gtwy_Browser` holds only the **last** browser call of the request, because tool results are keyed by name.

> [!WARNING]
> `built_in_tools` in the request replaces the agent's list (`built_in_tools or bridges.get("built_in_tools")` in `src/services/utils/getConfiguration.py`). Sending `["Gtwy_Browser"]` drops the agent's other built-in tools for that request.

> [!WARNING]
> One frozen tab renderer blocks every new CDP connection to its host, because Playwright attaches to every tab on connect. Calls then fail with "the browser did not answer" and every live view on that host spins. When REST answers in milliseconds but CDP hangs, see [Triage](#triage).

<!-- verify:ignore-start -->
> [!TODO] Needs a human: commit 7085fa5c on branch `claude/browser-frozen-tab-repair` adds a repair for this (`frozen_tabs.py`: ping each renderer, close the ones that do not answer, then reconnect). It is not in master. Merge it, or drop it?
<!-- verify:ignore-end -->

> [!TODO] Needs a human: if the 25 s setup budget runs out after `create_isolated_tab` but before `save_registry` in `_create_tab_on`, for example because Mongo is slow inside `restore_jar` (`with_timeout` allows 60 s), the new tab stays in Chrome with no registry record. Nothing ever closes it. Should cookie restore get its own short timeout?

Smaller traps:
- Ref ids are positions, not identities. An old `e5` that also exists in the newest map resolves to the new `e5`. Only a missing ref is reported as stale.
- A request without `thread_id` gets a fresh uuid, so it never reuses a tab or a saved login.
- Changing `Encreaption_key` or `Secret_IV` makes every stored blob unreadable. `restore_jar` logs an error and the users sign in again.
- `Helper` is imported inside `save_jar` / `restore_jar` on purpose: `src/services/utils/helper.py` imports back into this package.
- The module docstring of `src/db_services/browserCookieService.py` says Redis caches the cookies. That is stale; `cookies.py` is correct and nothing caches them.
- `TOKEN_TTL_DAYS` in `src/routes/v2/browserRouter.py` is not used. The expiry comes from `LIVE_LINK_TTL_DAYS`. The `reuse_only` parameter of `_try_host` is always False.

## Why it is built this way

- **Raw CDP contexts.** They are their own cookie jar, they survive a disconnect, and a fresh process finds them again by target id. Playwright's own contexts die with the connection (cdd31179). Playwright also reports every tab under `browser.contexts[0]`, so tab lookup goes by target id (`_rescan`).
- **Attach, never restart** (1b091990). The old code created a session on every connect failure, and the reaper and "tab lost" cleanup released the session by id. Probing showed both restart Chrome, so with two or three threads browsing, one hiccup restarted Chrome for everyone. The live link checks the session for the same reason.
- **Pool with 3 tabs per Chrome** (5d8512cb). One host used to carry every conversation, with a cap of 10. On 1.5 GB containers (four per 2 vCPU VM), three real shopping or login pages put a host at 80% memory and 1.8 cores, mostly from Steel logging every request of every page. Its live view stopped streaming for every tab. Raise the cap only after the hosts get more memory and Steel's per-request logging is turned down.
- **Timeout budget** (90c7205d, ecb0de37). The steps used to add up to more than 60 s, with four 10 s connect attempts plus a session create plus a 30 s navigate. The call that tried to repair a frozen Chrome was always cancelled. Now setup and page time are separate budgets, and setup and page failures return different messages. The timeouts that started this were a wedged Chrome, not the budget.
- **Permanent live link** (e541f072). A Steel link names one tab by id. Once that tab closes, Steel's player retries forever and shows "Session connecting..." with no explanation. A gtwy link resolves the tab when someone opens it. Rounding the expiry to the day keeps one link per conversation per day.
- **Mongo only for cookies.** A Redis cache in front was tried and removed. The blob is read about once per conversation, so the cache saved almost nothing, while it gave a login a second home and could serve a copy older than Mongo's. The first version had a `GTWY_BROWSER_PERSIST_COOKIES` flag. Now saving is on whenever the encryption keys exist. <!-- verify:ignore -->
- **Downloads denied** (253b042c). Amazon answered a search URL with a file (anti-bot behaviour against a datacenter IP). The download aborted the navigation, and every retry failed the same way. Errors now carry the first line of Playwright's message, because Playwright names every exception `Error`.
- **Every result carries `live_url`** (60f8b8a3), so the UI can show the tab at any time, not only during a login. The description tells the model to put it in a reply only when it asks the user to act.

## Changing it safely

- **New action:** add it to `BROWSER_ACTIONS` (the schema enum is built from it). Add a `do_*` function in `actions.py` that returns `(response, ref_map)` with a fresh snapshot, add a branch in `_dispatch`, and describe it in `DESCRIPTION`. If it can reach a new URL, call `ensure_url_allowed`. The handoff gate already blocks it during a login.
- **Steel calls:** keep every Steel interaction in `steel_client.py`. Do not add a release. Auth to Steel is not wired: `_request` sends no header, and the CDP connect sends none either. The first commit message (1024b926) claims gtwy sends `X-Steel-Api-Key`, but master does not. <!-- verify:ignore -->
- **Timeouts:** keep setup (25 s) plus navigate (30 s) under the 60 s ceiling. Keep the worst-case connect (one 7 s attempt, or 2 × 5 s for a fresh Chrome) plus the 5 s lock wait plus Steel REST inside 25 s.
- **Registry or thread-state shape:** old and new pods read the same Redis JSON during a deploy, so add fields and do not rename them. The cookie document has `version: 1` for format changes.
- **Tests:** this repo has no automated tests for this area. The pure functions are built to be testable: `parse_aria_snapshot`, `truncate`, `pick_current_session`, `should_retry_connect`, `reconcile_registry`, `evictable_tabs`. Use the runbook below.
- **Not built yet:** autoscaling the pool, placement by real host load (today it is by tab count), any UI or API to clear saved logins, and saving localStorage or IndexedDB (cookies only, so sites that keep the session elsewhere ask again).

### Runbook: run Steel

```yaml
services:
  steel:
    image: ghcr.io/steel-dev/steel-browser-api:latest
    ports: ["3000:3000", "9223:9223"]   # 3000 REST + CDP proxy + live view; 9223 raw DevTools, never public
    environment:
      DOMAIN: "steel.yourhost.com:3000"  # host the USER's browser can reach (used in debugUrl)
      CDP_DOMAIN: "steel.yourhost.com:9223"
      CHROME_HEADLESS: "true"
    shm_size: "2g"
    restart: unless-stopped
```

<!-- verify:ignore-start -->
Steel has no auth of its own, so the ingress in front of it carries the policy:
1. `/v1/sessions/debug` and `/v1/sessions/cast` stay public, but the ingress rejects any `pageId` that is not 32 hex chars. Without this rule, `/v1/sessions/debug` with no `pageId` serves an interactive picker over every conversation's tab, including one mid-login.
2. Everything else under `/v1/`, including session create and release and `/v1/devtools/*`, is reachable only from the gateway's egress addresses.
3. `/v1/health` is reachable from the load balancer only.
<!-- verify:ignore-end -->

### Runbook: manual verification

<!-- verify:ignore-start -->
1. Start Steel. `curl -X POST localhost:3000/v1/sessions` returns `id`, `websocketUrl` and `debugUrl`. Open `debugUrl` and click inside the page. Only do this on a Steel nobody else uses: creating a session restarts Chrome.
2. Set `STEEL_API_URLS` (or `STEEL_API_URL`) and start gtwy.
3. Send a stream completion with `built_in_tools: ["Gtwy_Browser"]` and "Open https://example.com and tell me the main heading". Expect `tool_call` (navigate), then a `tool_result` whose snapshot has `heading "Example Domain"`, then the answer.
4. Multi-step: "Go to https://duckduckgo.com, search for 'steel browser', list the first 3 result titles".
5. Two conversations: send the same request under two `thread_id` values. `redis-cli --scan --pattern '*gtwy_browser*'` shows the registry with two tab records. Log in on one thread; the other still sees a logged-out site. With every tab taken and none idle, the caller gets `all N browser tabs in the browser are in use by other conversations; retry in N seconds` (with a pool: `across N browsers`).
6. Handoff: "Log into https://github.com and tell me my username" gives a `browser_handoff` event. A `snapshot` in the same turn is refused. The next user message lets the agent continue.
7. Reaper: after 300 s idle, the tab closes and its cookies are saved. `GET /v1/sessions` still shows the same session.
8. Replaced Chrome: with a tab open, release the session in Steel's UI. The next browser call logs `Chrome on <host> was replaced` and opens a fresh tab without an error. The old `live_url` shows "No browser is open for this conversation".
<!-- verify:ignore-end -->

### Triage

If connects time out while gtwy leaves Chrome alone, look at Steel before the code. When its Chrome is wedged, the REST endpoints answer in milliseconds but the CDP websocket hangs, so health checks look fine. Check the api container's logs on the VM for that minute. Check whether Chrome answers on port 9223 inside the VM while the proxied websocket does not. A release from Steel's UI clears it, at the cost of every conversation's live login. gtwy recovers on the next call.

To forget one conversation's logins: wait until its tab closes (300 s idle, or 900 s mid-handoff), then delete its document (`scope_key` = `<org>:<thread>:<sub_thread>`) from `gtwy_browser_cookies`.
