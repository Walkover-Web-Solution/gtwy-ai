# Gtwy_Browser: giving an agent a real browser

`Gtwy_Browser` is a built-in tool. When it is in a bridge's `built_in_tools`
list, the model can drive a real Chromium running in a self-hosted
[Steel](https://github.com/steel-dev/steel-browser) container: navigate, read an
accessibility snapshot with element refs, click, type, press keys, scroll, take
and hand the browser to the user when a login or CAPTCHA is needed.

## How it works

```
model ──tool call──▶ gtwy (Gtwy_Browser tool) ──REST──▶ Steel /v1/sessions
                          │                     ──CDP (Playwright)──▶ Chromium in Steel
                          └── Redis: which tab belongs to which conversation + ref maps

one Chromium ── tab (own cookie jar) ── conversation A   logged in as user A
             ├─ tab (own cookie jar) ── conversation B   logged in as user B
             └─ tab (own cookie jar) ── conversation C   logged out
```

- **Snapshot loop.** Every action returns the page as an accessibility tree. Interactive
  elements carry refs like `[e5]`. The model acts on a ref, gets a fresh snapshot, repeats.
  Refs are only valid for the latest snapshot; a stale ref returns an error asking for a new snapshot.
- **One tab per conversation, with its own logins.** Steel open source runs one Chrome per
  container, and gtwy gives each conversation its own tab inside it. Every tab is created with raw
  CDP (`Target.createBrowserContext` + `Target.createTarget`), so it has its own cookie jar: two
  conversations can be signed in to the same site as different people, and neither sees the other's
  session. Coming back to an older conversation reuses that conversation's tab, still logged in.
  Tabs are tracked in `nd_gtwy_browser_registry` by Chrome target id, so any gtwy pod can find the
  right tab. Up to 10 conversations can hold a tab at once; beyond
  that, a conversation whose tab has been idle past the 300 s idle timeout is recycled, and if
  none are idle the caller gets a clear "all tabs are in use" error.
  A reaper on every pod closes idle tabs and releases the Steel session once no tab is left.
- **Why raw CDP and not `browser.new_context()`.** Playwright owns the contexts it creates and
  destroys them when the connection closes, which would drop a user's login on every reconnect or
  pod restart. Raw CDP contexts survive, and a fresh process can find their tabs again by target id.
  For the same reason the code never calls `browser.close()`, only stops the Playwright driver.
- **Login handoff.** When a navigation lands on a page that looks like a sign-in page (login-style URL,
  known identity-provider host, or a visible password field) the tool starts the handoff automatically
  and adds `login_required`, `live_url` and instructions to the result. The model can also start it
  explicitly. The live view is pinned to that conversation's own tab with `?pageId=<target id>`, so a
  user only ever sees and types into their own page. The action `request_user_action` returns the live view URL
  (Chrome DevTools frontend, interactive) and emits an SSE event `browser_handoff`. The chat UI
  shows it in an iframe so the user logs in themselves. No snapshots are taken during the handoff,
  so typed credentials never reach the model or the logs. The handoff ends when the user sends
  the next message or the model navigates.
- **Remembered logins.** A tab's cookies are exported
  just before it closes and restored into the next tab that conversation opens, so a user who
  logged in yesterday is still signed in today. All of that tab's sites come back together, since
  cookies are saved per jar rather than per site.

  **MongoDB is the only place a login lives.** The collection is `gtwy_browser_cookies`, one
  document per conversation, holding nothing but an encrypted blob:

  ```
  { "scope_key": "<org_id>:<thread_id>:<sub_thread_id>",
    "org_id": ..., "bridge_id": ...,
    "cookies": "<Helper.encrypt(json)>", "cookie_count": 6, "version": 1,
    "updated_at": ..., "expires_at": ... }
  ```

  Two indexes are created at startup: unique on `scope_key`, and a TTL index on `expires_at` with
  `expireAfterSeconds=0`. The deadline lives in the document, so changing
  the retention value needs no index rebuild. Mongo's TTL sweep runs about once a
  minute, so reads also check expiry rather than trusting deletion. If Mongo is unreachable the
  turn still succeeds and the user simply signs in again.
  A Redis cache in front of this was tried and removed. The blob is read once per tab creation,
  roughly once per conversation, so the cache saved almost nothing while giving a login a second
  home and a way to serve a copy older than Mongo's.

  Steel's own `GET /v1/sessions/{id}/context` cannot see these jars, so export and import go through
  CDP `Storage.getCookies` and `Storage.setCookies` with the jar's `browserContextId`.
  To forget a conversation's logins, delete its document from `gtwy_browser_cookies`. Note that
  deleting the document is not enough on its own while that conversation's tab is still open:
  closing it saves the same cookies back, so clear the live jar too.
- **Loop limit.** With `Gtwy_Browser` enabled and no explicit `settings.maximum_iterations`,
  the tool loop limit is 25 instead of 3.

Code lives in `src/services/utils/built_in_tools/browser/`.

## Run Steel

```yaml
# docker-compose.yml
services:
  steel:
    image: ghcr.io/steel-dev/steel-browser-api:latest
    ports:
      - "3000:3000"   # REST, CDP proxy, live view
      - "9223:9223"   # raw Chrome DevTools; never expose publicly
    environment:
      DOMAIN: "steel.yourhost.com:3000"       # host the USER's browser can reach (used in debugUrl)
      CDP_DOMAIN: "steel.yourhost.com:9223"
      CHROME_HEADLESS: "true"
    shm_size: "2g"
    restart: unless-stopped
```

**Steel ships with no authentication of its own, so the ingress in front of it carries the
policy.** Three rules, and the first is the important one:

1. `/v1/sessions/debug` and `/v1/sessions/cast` stay public, but reject any request whose `pageId`
   is not 32 hex characters. Without this, `/v1/sessions/debug` with no `pageId` serves the
   interactive multi-tab player, which lets anyone with the URL watch and click inside every
   conversation's browser, including one where a user is mid-login.
2. Everything else under `/v1/`, including session create, release and `/v1/devtools/*`, is
   restricted to the gateway's egress addresses.
3. `/v1/health` from the load balancer only.

The shared-secret header is not wired yet, so restrict by network address for now. Adding
`X-Steel-Api-Key` to `steel_client._request` and the CDP connect is about three lines when you enforce it.

## Configure gtwy

```
STEEL_API_URL=https://api-1.browser.embarko.ai   # empty disables the tool
```

That is the only environment variable. Everything else is a constant in the code, because it
describes how the browser behaves rather than where it lives:

| Constant | Where | Value |
|---|---|---|
| `IDLE_TIMEOUT_SECONDS` | `session_store.py` | 300, close a tab after 5 minutes of silence |
| `HANDOFF_TIMEOUT_SECONDS` | `session_store.py` | 900, longer while a user is signing in |
| `MAX_TABS` | `session_store.py` | 10 conversations browsing at once per container |
| `SNAPSHOT_MAX_CHARS` | `actions.py` | 15,000, cuts huge pages |
| `COOKIE_TTL_SECONDS` | `cookies.py` | 30 days a saved login lives in MongoDB |


Install the Python dependency (CDP client only, no browser download):

```bash
pip install -r req.txt
```

Enable the tool on a bridge by adding `"Gtwy_Browser"` to its `built_in_tools`, or pass
`"built_in_tools": ["Gtwy_Browser"]` in the request body.

## Tool schema seen by the model

| Param | Used by | Notes |
|---|---|---|
| `action` | all | `navigate`, `snapshot`, `click`, `type`, `press`, `scroll`, `back`, `request_user_action` |
| `url` | navigate | http(s) only; private and internal hosts are blocked |
| `ref` | click, type | e.g. `e12`, from the latest snapshot |
| `text` | type | replaces the field's value; password fields are refused |
| `key` | press, type | `Enter`, `Escape`, `Tab`, `ArrowDown`... |
| `direction` | scroll | `up` or `down` |
| `message` | request_user_action | what the user must do in the live view |

Every result has the shape `{"response": {...}, "status": 1}` on success or
`{"response": {"error": "..."}, "status": 0}` on failure, like every other gtwy tool.
Snapshot text is wrapped in `<<<UNTRUSTED_WEB_CONTENT>>>` markers.

## SSE events

- `tool_call` / `tool_result` as for any tool.
- `browser_handoff`: `{"event": "browser_handoff", "live_url", "message", "call_id"}`.
  `live_url` is pinned to this conversation's own tab, so the user only sees their own page.

The same value also arrives as `live_url` inside the tool result (`login_required` is `true` there),
and for non-streaming callers at `response.data.tools_data.Gtwy_Browser.live_url`. Read one of those
rather than parsing the model's reply text: the URL is long and a model may reformat or truncate it.

```js
// in the chat UI's SSE handler
if (event.event === "browser_handoff") {
  showLoginPanel({
    message: event.message,                       // "This page requires you to log in."
    url: event.live_url,                          // pinned to this conversation's tab
  });
}

// the panel: an inline live browser plus a Done button
// <iframe src={url + "&clipboardBridge=true"}
//         allow="clipboard-read; clipboard-write"
//         style="width:100%;height:600px;border:0" />
// <button onClick={() => sendMessage("Done, I have logged in.")}>Done</button>
```

The user's next message on the same thread ends the handoff and the agent continues. If you cannot
embed an iframe yet, render `live_url` as a plain "Log in" link that opens in a new tab.

## Redis keys

```
nd_gtwy_browser_registry                            the shared Chrome: steel session + one record
                                                    per open tab {target_id, browser_context_id,
                                                    last_used_at, handoff_active}
nd_gtwy_browser_thread_<org>:<thread>:<sub_thread>  per-conversation ref map and handoff flags
lock_gtwy_browser_registry, lock_gtwy_browser_reaper  short SETNX locks
```

## Manual verification

1. Start Steel. `curl -X POST localhost:3000/v1/sessions` returns `id`, `websocketUrl`, `debugUrl`.
   Open `debugUrl` in a browser and confirm you can click inside the page. Release with
   `curl -X POST localhost:3000/v1/sessions/<id>/release`.
2. Set `STEEL_API_URL`, start gtwy.
3. Chat completion with `stream: true`, `built_in_tools: ["Gtwy_Browser"]`, prompt
   "Open https://example.com and tell me the main heading". Expect `tool_call` (navigate),
   a `tool_result` whose snapshot contains `heading "Example Domain"`, then the answer.
4. Multi-step: "Go to https://duckduckgo.com, search for 'steel browser', list the first 3 result titles".
5. Two conversations at once: send the same request under two different `thread_id` values. Both
   succeed, each in its own tab, and `redis-cli --scan --pattern '*gtwy_browser*'` shows one registry
   with two tab records. Cookies do not cross: log in on one thread and the other still sees a
   logged-out site. With all tabs held and no idle tab, the caller gets
   `all N browser tabs are in use by other conversations; retry in N seconds`.

6. Reaper: after the idle timeout the tab is closed and its cookies saved; once no tab is left the Steel session is released.
7. Handoff: "Log into https://github.com and tell me my username" produces a `browser_handoff`
   event with a live URL; `snapshot` in the same turn is refused; the next user message lets it continue.

## Not in this POC

- A pool of Steel containers, and scaling them with demand, for more than ~10 concurrent conversations.
- A user-facing control to clear saved logins. Nothing exposes one yet.
- Local storage and IndexedDB are not saved, only cookies. Sites that keep their session outside
  cookies will still ask the user to log in again.
- Screenshot results are returned as base64 JPEG text; vision-model image attachment is not wired.
