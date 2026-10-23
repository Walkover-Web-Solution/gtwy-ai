# Gtwy_Browser: giving an agent a real browser

`Gtwy_Browser` is a built-in tool. When it is in an agent's `built_in_tools` list, the model drives
a real hosted Chromium: navigate, read an accessibility snapshot with element refs, click, type,
press keys, scroll, go back, and hand the browser to the user when a login or CAPTCHA is needed.

Each agent picks the service that hosts its browsers (`gtwy_browser_settings.provider`, see
[Browser services](#browser-services)): Browser Use (the default), Browserbase, Steel Cloud, Kernel or
Anchor Browser. Only their browser infrastructure is used (each returns a CDP URL); no agent API is:
the gtwy agent's own model decides every click. Until October 2026 the tool ran on
self-hosted Steel. That backend was removed because one shared Chrome per host made every
conversation's tabs hang, crash and get blocked together, and its live view could show other
conversations' tabs.

## How it works

```
model ──tool call──▶ gtwy (Gtwy_Browser tool) ──REST──▶ the agent's browser service   (start / stop)
                          │                     ──CDP (Playwright)──▶ that conversation's browser
                          └── Redis: which browser (and service) belongs to which conversation + ref maps
                          └── Mongo: which profile on which service holds which conversation's logins

conversation A ── its own browser (profile A: logged in as user A)
conversation B ── its own browser (profile B: logged in as user B)
conversation C ── its own browser (proxy off for its agent, logged out)
```

- **Snapshot loop.** Every action returns the page as an accessibility tree. Interactive elements
  carry refs like `[e5]`. The model acts on a ref, gets a fresh snapshot, repeats. Refs are only
  valid for the latest snapshot; a stale ref returns an error asking for a new snapshot.
- **New tabs are followed.** The agent sees one page at a time. When a click, a key press or typing
  makes the site open a new tab (Flipkart and Amazon open products this way), the agent is moved to
  that tab, gets its snapshot and `opened_new_tab: true`. The earlier tab stays open in the
  background; the user sees both in the live view.
- **One browser per conversation.** The key is `<org_id>:<thread_id>:<sub_thread_id>`. Each
  conversation gets a whole Chromium of its own, so its live view can only show its own pages, and a
  heavy, frozen or crashed page touches no one else. Any gtwy pod can drive any conversation's
  browser: each pod keeps one CDP connection per browser it has touched, and two pods on one browser
  work side by side. Coming back to a conversation reuses its running browser (about 1 s). A new one
  takes about 10 s (profile, start, connect).
- **Stopping is gtwy's job.** A hosted browser bills while it runs and does not stop when CDP
  disconnects or when it sits idle; only gtwy's stop call or the service's own timeout ends it. The
  reaper on every pod (one acts per tick, under a Redis lock) stops browsers idle past 300 s (900 s
  while a user is in the handoff), each through its own service. It also stops any running browser
  tagged `gtwy_env=<ENVIRONMENT>` that no conversation points at and that is older than 5 minutes (a
  record lost to a crash mid-start), on every service that can filter by tag (all but Steel). Give
  every deployment its own `ENVIRONMENT` and its own keys, or their reapers stop each other's browsers.
- **A browser stopped from outside** (dashboard, timeout) is noticed on the next call: the connect
  fails, the service reports it stopped, and a new browser starts with the same profile.
- **Per-org limit.** An org may have at most `MAX_BROWSERS_PER_ORG` (20) browsers open at once, across
  every service, so one busy org cannot use up an account's concurrent browsers. At its
  limit, the org's quietest browser idle past its limit is stopped to make room. If none is idle, the
  model gets `your organization already has N browsers open...; retry in S seconds`. An account's
  own limit answers `the <service> browser service is running as many browsers as its plan allows;
  retry in 30 seconds`.
- **Default context, never `new_context()`.** The browser's default context is where the
  conversation's profile is loaded; a fresh context would start logged out. `browser.close()` on a
  CDP connection also closes what Playwright opened in it, so it is only called for a browser that
  is stopped or lost.
- **Remembered logins: profiles, per service.** Each conversation gets a profile on each service it
  uses (a Browser Use profile, a Browserbase context, a Steel, Kernel or Anchor profile). gtwy stores
  only its id in Mongo (`gtwy_browser_profiles`, unique on `scope_key` + `provider`), so logins made
  on one service are not on another. The browser's cookies (session cookies too) and local storage
  are saved into the profile when it stops and are there in the next browser. Some services need a
  few seconds to save it (`PROFILE_SAVE_SECONDS`); a new browser waits for that, and a profile still
  saving after a retry answers `saving your logins from the last browser; retry in 5 seconds`. A
  profile deleted on the service's side is replaced on the next start. Whichever browser stops last
  overwrites a profile, which is one reason profiles are per conversation.
- **Logins from the Steel days.** Steel kept encrypted cookies in `gtwy_browser_cookies`. When a
  conversation opens its first Browser Use browser, those cookies are loaded into it once and the
  document is deleted (`legacy_cookies.py`). The collection's TTL index removes the rest within 30
  days. Then delete `legacy_cookies.py`, `browserCookieService.py` and the collection.
- **Proxy on by default.** Every browser goes through its service's residential proxy in India
  (`DEFAULT_PROXY_COUNTRY` in `provider_base.py`). It gets past much more bot protection but is
  billed per GB (see the services table). An agent picks another country with
  `gtwy_browser_settings: {"proxy_country": "us"}` or turns it off with `"none"`, on the agent or in
  the request body. The setting applies when the conversation's next browser starts. To check it,
  ask the browser to open https://ipinfo.io/json: it shows India with the default proxy. Every browser is tagged `gtwy_org`, `gtwy_env` and
  `gtwy_proxy` on services that take tags, so cost per org can be read there.
- **Stealth.** Browser Use runs a hardened Chromium with stealth on by default and solves supported
  CAPTCHAs. In the POC, bot.sannysoft.com showed 0 failed checks (plain headless Chrome: 4), and
  g2.com, indeed.com, zillow.com and nike.com loaded with the proxy where plain Chrome was blocked.
  The other services' stealth and CAPTCHA solving are listed in the services table.
- **Login handoff.** When a navigation lands on a page that looks like a sign-in page (login-style
  URL, known identity-provider host, or a visible password field) the tool starts the handoff on its
  own and adds `login_required`, `live_url` and instructions to the result. The model can also start
  it with `request_user_action`. The `live_url` opens the conversation's live browser, where the user
  logs in themselves, and a `browser_handoff` SSE event is emitted. No snapshots are taken during the
  handoff, so typed credentials never reach the model or the logs. It ends when the user sends the
  next message or the model navigates.
- **Loop limit.** With `Gtwy_Browser` enabled and no explicit `settings.maximum_iterations`, the
  tool loop limit is 25 instead of 3.

Code lives in `src/services/utils/built_in_tools/browser/`:

| File | Role |
|---|---|
| `tool.py` | entry point `call_gtwy_browser(args, ctx)`, picks the service, handoff, live link in every result |
| `sessions.py` | one browser per conversation on its service: start, reuse, stop, per-org limit, reaper logic, live target |
| `providers.py` | the services an agent can pick, which one it picked, which are set up |
| `provider_base.py` | what every service client shares: errors, REST call, proxy country, keeping keys out of URLs |
| `browser_use_client.py`, `browserbase_client.py`, `steel_cloud_client.py`, `kernel_client.py`, `anchor_client.py` | one REST client per service, same functions and same browser view |
| `connection.py` | per-pod Playwright driver and CDP connections, the conversation's page |
| `profiles.py`, `legacy_cookies.py` | logins: profile per conversation, one-time import of Steel cookies |
| `session_store.py` | per-conversation ref map and handoff state, idle limits |
| `live_link.py` | the signed, short-lived live link |
| `actions.py`, `snapshot.py`, `schema.py`, `security.py` | actions, aria snapshot to refs, tool schema, URL guard |
| `reaper.py` | the background loop |

## Browser services

An agent picks its service with `gtwy_browser_settings.provider`, kept on its `connected_tools` entry
of type `built_in_tools` (next to the web search filters; an older top-level `gtwy_browser_settings`
field and the request body are read too, the request winning key by key):

```json
{"type": "built_in_tools", "built_in_tools": ["Gtwy_Browser"],
 "gtwy_browser_settings": {"provider": "browserbase", "proxy_country": "in"}}
```

No `provider` means Browser Use. A service whose key is not set on the server is never replaced by
another (logins are per service): the tool answers `the <service> browser service is not set up on
this server yet...`, and an unknown name lists the valid ones. `GET /browser/providers` (gtwy login)
returns `{"providers": [{id, label, configured, default}]}` for the agent settings picker.

A running browser keeps its service: a changed choice applies to the conversation's next browser,
after the current one stops (5 idle minutes). Switching on the spot would make two agents sharing a
thread (a transfer) stop each other's browsers on every call.

| | Browser Use | Browserbase | Steel Cloud | Kernel | Anchor Browser |
|---|---|---|---|---|---|
| `provider` | `browser_use` | `browserbase` | `steel` | `kernel` | `anchor` |
| Keys | `BROWSER_USE_API_KEY` | `BROWSERBASE_API_KEY` + `BROWSERBASE_PROJECT_ID` | `STEEL_API_KEY` | `KERNEL_API_KEY` | `ANCHOR_API_KEY` |
| Browser time | ~$0.02/h | $0.10-0.12/h | $0.08-0.10/h | ~$0.48/h headful (see below) | ~$0.05/h + $0.01 per browser |
| Proxy traffic | ~$4.9/GB | $10-12/GB | $6-10/GB | included | $8/GB |
| Logins kept in | profile | context (`persist`) | profile, created by the first session | profile (`save_changes`) | profile, by name |
| Live view | `liveUrl` | `GET /sessions/{id}/debug`, the conversation's tab | `debugUrl?interactive=true` | `browser_live_view_url` | `live_view_url` |
| Lifetime | 240 min | 240 min (`keepAlive`) | 60 min, 15 on the lowest plan | until 15 min with no connection | 240 min, idle 15 min |
| Stray sweep | `gtwy_env` metadata | `userMetadata` | none (no tags) | `tags` | `tags` |
| Proxy needs | | a paid plan (free plan: 402) | a funded account | a paid plan (free plan: 403) | credits |
| Start (measured) | ~10 s | ~9 s | not measured | ~10 s | not measured |

Notes per service:

- **Browserbase** ends a session when its CDP client disconnects unless `keepAlive` is set (the free
  plan accepts it). Its free plan refuses the proxy (402), so an agent on it needs
  `"proxy_country": "none"` or a paid plan; the model gets the service's own reason. Session
  recording and logging are off. Its live view is per tab; the conversation's current tab is shown.
- **Steel** sessions have no tags, so the stray sweep skips Steel: use one Steel key per environment.
  Its lowest plan caps a session at 15 minutes; a longer conversation gets a new browser that opens
  with the same saved logins. Its CDP URL needs the account key, added only when connecting.
- **Kernel** browsers go into zero-cost standby only when nothing is connected. gtwy pods keep their
  CDP connection open, so a Kernel browser bills headful time until the reaper stops it. Its timeout
  (`INACTIVITY_TIMEOUT_SECONDS`, 900) only cleans up after a crash. The residential proxy is a Kernel
  object, created once per country and found again by name (`gtwy-residential-in` is accepted); its
  free plan refuses proxies (403), so a free-plan agent needs `"proxy_country": "none"`.
- **Anchor** names profiles instead of numbering them; each conversation gets `gtwy-<hash of its
  key>`. Recording is off. Its connect URL authenticates with the account key, so a key in any URL it
  returns is stripped before Redis and added back only when connecting; a live view URL that carries
  a key is never handed out.

No account key is stored in Redis or shown to the model: CDP URLs are stored without keys, every
error text is passed through `scrub()`, and only Browser Use's own live URL (scoped to one browser)
may stand in for the signed gtwy link when `GTWY_PUBLIC_URL` is missing.

Rollout: deploy the code first and set a new service's keys only once every pod runs it, so an older
pod never meets a record of a service it does not know. Check each service live before offering it
in the UI (see Manual verification).

## Configure gtwy

These settings come from the environment:

```
BROWSER_USE_API_KEY=bu_...              # each key turns its service on; empty: that service is off
BROWSERBASE_API_KEY=bb_live_...
BROWSERBASE_PROJECT_ID=...
STEEL_API_KEY=ste-...
KERNEL_API_KEY=sk_...
ANCHOR_API_KEY=sk-...
GTWY_PUBLIC_URL=https://dev-api.gtwy.ai # for the signed live link
```

The tool is offered when at least one service is set up (or the agent named one, so the model can
say it is not set up). Use separate keys (and a separate Browserbase project) per environment, and a
unique `ENVIRONMENT` value per deployment. Everything else is a constant in the code:

| Constant | Where | Value |
|---|---|---|
| `IDLE_TIMEOUT_SECONDS` | `session_store.py` | 300, stop a browser after 5 minutes of silence |
| `HANDOFF_TIMEOUT_SECONDS` | `session_store.py` | 900, longer while a user is signing in |
| `MAX_BROWSERS_PER_ORG` | `sessions.py` | 20 browsers open at once per org, all services together; 0 = no org limit |
| `BROWSER_TIMEOUT_*`, `SESSION_TIMEOUT_MS`, `MAX_DURATION_MINUTES` | each `*_client.py` | hard lifetime of one browser per service |
| `PROFILE_SAVE_SECONDS` | each `*_client.py` | how long a stopped browser's profile takes to save |
| `DEFAULT_PROXY_COUNTRY` | `provider_base.py` | `in` |
| `LIVE_LINK_TTL_MINUTES` | `live_link.py` | 15, how long a live link can be opened |
| `REQUIRE_VIEWER_LOGIN` | `live_link.py` | False; True makes only `/browser/live-session` hand out the view |
| `SNAPSHOT_MAX_CHARS` | `actions.py` | 15,000, cuts huge pages |

The Python dependency is `playwright` as a CDP client only; no browser download is needed.

Enable the tool on an agent by adding `"Gtwy_Browser"` to its `built_in_tools`, or pass
`"built_in_tools": ["Gtwy_Browser"]` in the request body. The proxy is on (India) by default; set
`gtwy_browser_settings: {"proxy_country": "none"}` on an agent (or in the request body) to turn it
off, or another two-letter country to change it.

## Tool schema seen by the model

| Param | Used by | Notes |
|---|---|---|
| `action` | all | `navigate`, `snapshot`, `click`, `type`, `press`, `scroll`, `back`, `request_user_action` |
| `url` | navigate | http(s) only; private and internal hosts are blocked |
| `ref` | click, type | e.g. `e12`, from the latest snapshot |
| `text` | type | replaces the field's value; password fields are refused |
| `key` | press, type | `Enter`, `Escape`, `Tab`, `ArrowDown`... For `type`, the key is pressed where the focus is after typing |
| `direction` | scroll | `up` or `down` |
| `message` | request_user_action | what the user must do in the live view |

Every result has the shape `{"response": {...}, "status": 1}` on success or
`{"response": {"error": "..."}, "status": 0}` on failure, like every other gtwy tool.
Snapshot text is wrapped in `<<<UNTRUSTED_WEB_CONTENT>>>` markers.

## SSE events and the live view

- `tool_call` / `tool_result` as for any tool.
- `browser_handoff`: `{"event": "browser_handoff", "live_url", "message", "call_id"}`.

**Every browser result carries `live_url`**, so the interface can show this conversation's browser at
any moment. It points at gtwy: `/browser/live/<token>` resolves the conversation's browser when it is
opened and redirects to its live view, or says "No browser is open" instead of spinning. The token is
signed with `SecretKey` and names the conversation's org and, when known, the signed-in user who is
chatting (`u`: the gtwy user, or the chatbot's end user). It expires after
`LIVE_LINK_TTL_MINUTES` and every result mints a fresh one. Without `GTWY_PUBLIC_URL` the tool
falls back to the browser's own live URL, which lets anyone holding it control that browser. Read the
link from the streamed `tool_result` or from `response.data.tools_data.Gtwy_Browser.live_url`, not
from the model's reply text.

```js
// in the chat UI's SSE handler
if (event.event === "browser_handoff") {
  showLoginPanel({ message: event.message, url: event.live_url });
}
// the panel: an inline live browser plus a Done button
// <iframe src={liveViewUrl} style="width:100%;height:600px;border:0" />
// <button onClick={() => sendMessage("Done, I have logged in.")}>Done</button>
```

### Who may open the live view

Whoever opens a live view can watch and drive that conversation's browser, including a page where
its user is signed in. Browser Use has no login check on it: its live URL is
`https://live.browser-use.com?wss=<cdp url>`, and the CDP URL answers without the API key. The browser
id inside it is a random UUID (a made-up one gets `502 vm not found`), so it cannot be guessed and
never reaches another conversation's browser, but whoever holds it controls that browser.

The plain link checks only the token, so it is short-lived. The UI should instead ask for the view as
the signed-in user:

```js
// token = the last path segment of live_url
const res = await fetch(`${GTWY}/browser/live-session/${token}`, {
  headers: { Authorization: userToken },   // the same header the UI sends to the chat API:
});                                         // a gtwy login, or the chatbot end user's token
if (res.ok) iframe.src = (await res.json()).live_url;   // 403: not this user's browser, 404: none open
```

`/browser/live-session/<token>` returns the view only when the caller's org is the conversation's org
and, when the token names a user, the caller is that user. A teammate in the same org, another chatbot
end user, or another org gets 403. Once every UI uses it, set `REQUIRE_VIEWER_LOGIN = True` in `live_link.py`:
the plain link then answers "Open the browser from the chat" and a forwarded or leaked link opens
nothing. Do not log the returned URL or put it in the chat.

## Redis keys

Records written before services existed have no `provider` and are Browser Use; key names did not
change with the services.

```
nd_gtwy_browser_bu_<org>:<thread>:<sub_thread>      the conversation's browser: service, id, CDP and live URL
                                                    (no account key), profile id, proxy, page target, last
                                                    use, handoff flag
nd_gtwy_browser_saving_<service>_<key>              a stopped browser's profile is still being saved
nd_gtwy_browser_bu_index                            every conversation with a browser (the reaper walks it)
nd_gtwy_browser_bu_org_<org>                        the org's conversations with a browser (per-org limit)
nd_gtwy_browser_thread_<org>:<thread>:<sub_thread>  ref map of the last snapshot and handoff flags
lock_gtwy_browser_bu_<key>, lock_gtwy_browser_bu_org_<org>, lock_gtwy_browser_reaper   short SETNX locks
```

## Timeouts

A browser call is capped at 60 s. Getting the browser ready (locks, profile, starting a browser, CDP
connect) is capped at 25 s on its own, so a slow start never eats the page's time; the page then gets
its 30 s. A connect that fails is reported as "the browser did not answer; try again". A browser that
was just started gets three connect attempts, one second apart.

## Costs (Browser Use, measured October 2026)

Other services' prices are in the services table (from their pricing pages, not measured yet).

- Browser time: $0.02 per browser-hour, billed per minute. With the 5-minute idle stop, a 10-minute
  conversation costs about $0.005.
- Residential proxy (on by default): about $4.9 per GB. A shopping home page is
  2-3 MB. Without the proxy, traffic cost about $0.20 per GB.
- Plan: 10 concurrent browsers free, 50 after a one-time $100 top-up.

## Manual verification

1. Set `BROWSER_USE_API_KEY` and `GTWY_PUBLIC_URL`, start gtwy.
2. Chat completion with `built_in_tools: ["Gtwy_Browser"]`, prompt "Open https://news.ycombinator.com
   and tell me the top 3 titles". Expect a `navigate` tool call, a snapshot, then the answer. Send a
   second message on the same thread: the log shows no new "started browser use browser" line.
3. "Open the GitHub sign-in page so I can log in": the result has `login_required: true` and a
   `live_url`. Open it, log in, reply "done": the agent continues signed in.
4. Live view auth: `/browser/live-session/<token>` with the chatting user's login answers 200; another
   user, another org or no login answers 403/401.
5. After 5 idle minutes the reaper stops the browser and the old live link says "No browser is open".
   A new message on the thread starts a browser that is still logged in.
6. With `MAX_BROWSERS_PER_ORG = 2`, a third conversation in one org gets the org-limit error.
7. Live results (October 23 2026, through the real tool code with Redis and Mongo faked; the scripts
   `test_live_services.py` and `hold_live.py` live in that session's scratchpad):
   - **Browserbase** (free plan, proxy off): 14/14. Start, click, back, reuse, the live view (no key
     in it, opens without login, no frame-blocking headers, typing an address there moves the
     browser the tool sees), the `userMetadata` stray filter, and both a session and a persistent
     cookie surviving a stop and a new browser.
   - **Kernel** (free plan, proxy off): 14/14, the same checks; its live view shows the whole
     browser and takes clicks and keys.
   - **Anchor**: not run: the account had no credits ("Insufficient credits balance" even without a
     proxy). **Steel**: not run: no key.
   - Not checked anywhere yet: the India proxy on a paid Browserbase or Kernel plan, the account
     limit (429), and a session left idle for 10 minutes on Browserbase.
8. For each new service, before offering it: set its key, pick it on an agent
   (`gtwy_browser_settings.provider`) and repeat steps 2-5, plus https://ipinfo.io/json shows India
   (and not with `"proxy_country": "none"`), the live view works inside an iframe and its URL has no
   account key, and picking an unset service gives the "not set up" error. Then check what the docs
   left open:
   - Browserbase: keep-alive works on the plan, and a session survives 10 idle CDP minutes.
   - Steel: the plan's longest timeout, how long a profile stays uploading (409), and that a timed-out
     session still saves its profile.
   - Kernel: an India residential proxy exists, and deleting a browser saves its profile.
   - Anchor: an unknown profile name with `persist` creates the profile, its `cdp_url` and
     `live_view_url` carry no `apiKey`, and its status values.

## Not done yet

- The UIs (gtwy-ui, chatbot) still open the plain link; they should call `/browser/live-session` and
  then `REQUIRE_VIEWER_LOGIN` can be set to True.
- The per-agent browser settings (`provider`, `proxy_country`) have no UI yet: Node must accept
  `gtwy_browser_settings` on the `built_in_tools` entry (its Joi item schema allows unknown keys
  today; add `provider` as an enum) and the agent screen needs a picker fed by `GET /browser/providers`.
- Browserbase, Steel, Kernel and Anchor are built from their docs and tested offline and against a
  local Chrome; each still needs the live checks in Manual verification step 7 with a real key.
- Profiles stay on Browser Use until deleted; nothing removes those of old conversations yet.
- Screenshot results are returned as base64 JPEG text; vision-model image attachment is not wired.
