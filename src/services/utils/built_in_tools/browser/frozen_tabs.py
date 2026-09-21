"""Find and close tabs whose renderer has frozen, over raw CDP, without Playwright.

Both Playwright and Steel's live view initialize every tab when they connect, and that step
waits for each tab's renderer to answer. One page that froze while loading therefore blocks
every new connection to that Chrome: the websocket opens, then nothing, while Chrome's own
browser process keeps answering instantly. Seen live on api-1 with a shopping site's orders
page whose title never left ``about:blank``.

The old reaction was to restart Chrome, which threw every conversation out. The reaction here
is surgical: talk to Chrome at the browser level, which still works, ask each tab's renderer
a trivial question with a short deadline, and close only the tabs that never answer. The
caller then forgets those tabs and connects again. Everything runs over one plain websocket
with our own request ids, because the whole point is not to depend on a client that attaches
to every page.
"""

import asyncio
import itertools
import json

from globals import logger

from . import steel_client

# A healthy renderer answers ``1`` in a few milliseconds; a frozen one never does. Probes run
# concurrently, so the repair costs about one deadline, not one per tab.
BROWSER_DEADLINE_SECONDS = 3.0
RENDERER_DEADLINE_SECONDS = 3.0


class _RawCDP:
    """Minimal CDP client: send commands, await the reply that carries the same id."""

    def __init__(self, ws):
        self._ws = ws
        self._ids = itertools.count(1)
        self._waiting: dict[int, asyncio.Future] = {}
        self._reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                future = self._waiting.pop(msg.get("id"), None)
                if future is not None and not future.done():
                    future.set_result(msg)
        except Exception as exc:
            for future in self._waiting.values():
                if not future.done():
                    future.set_exception(exc)

    async def send(self, method: str, params: dict | None = None, session_id: str | None = None, timeout: float = 5.0) -> dict:
        request_id = next(self._ids)
        message = {"id": request_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        future = asyncio.get_event_loop().create_future()
        self._waiting[request_id] = future
        await self._ws.send(json.dumps(message))
        try:
            reply = await asyncio.wait_for(future, timeout)
        finally:
            self._waiting.pop(request_id, None)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result") or {}

    async def close(self):
        self._reader.cancel()


async def _renderer_answers(cdp: _RawCDP, target_id: str) -> bool:
    attached = await cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
    session_id = attached["sessionId"]
    try:
        await cdp.send(
            "Runtime.evaluate", {"expression": "1", "returnByValue": True}, session_id=session_id, timeout=RENDERER_DEADLINE_SECONDS
        )
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        try:
            await cdp.send("Target.detachFromTarget", {"sessionId": session_id}, timeout=2.0)
        except Exception:
            pass


async def close_frozen_tabs(host: str) -> list[str] | None:
    """Close every page whose renderer does not answer. Returns their target ids.

    Returns None when Chrome's browser process itself does not answer, which no tab-level
    repair can fix; the caller should then just report the browser as unavailable.
    """
    try:
        import websockets
    except ImportError:  # pragma: no cover - dependency is in req.txt
        logger.warning("Gtwy_Browser: websockets is not installed; cannot look for frozen tabs")
        return []

    closed: list[str] = []
    try:
        async with websockets.connect(steel_client.cdp_ws_url(host), open_timeout=BROWSER_DEADLINE_SECONDS, max_size=None) as ws:
            cdp = _RawCDP(ws)
            try:
                try:
                    await cdp.send("Browser.getVersion", timeout=BROWSER_DEADLINE_SECONDS)
                except asyncio.TimeoutError:
                    logger.warning(f"Gtwy_Browser: Chrome on {steel_client.short_host(host)} does not answer at the browser level")
                    return None
                targets = (await cdp.send("Target.getTargets", timeout=BROWSER_DEADLINE_SECONDS))["targetInfos"]
                pages = [t for t in targets if t.get("type") == "page"]
                answers = await asyncio.gather(*(_renderer_answers(cdp, t["targetId"]) for t in pages), return_exceptions=True)
                for target, answered in zip(pages, answers, strict=True):
                    if answered is True:
                        continue
                    reason = "frozen renderer" if answered is False else f"probe failed: {answered.__class__.__name__}"
                    try:
                        await cdp.send("Target.closeTarget", {"targetId": target["targetId"]}, timeout=BROWSER_DEADLINE_SECONDS)
                        closed.append(target["targetId"])
                        logger.warning(
                            f"Gtwy_Browser: closed tab {target['targetId'][-6:]} on {steel_client.short_host(host)} "
                            f"({reason}) url={target.get('url', '')[:80]}"
                        )
                    except Exception as exc:
                        logger.warning(f"Gtwy_Browser: could not close frozen tab {target['targetId'][-6:]}: {exc}")
                    context_id = target.get("browserContextId")
                    if context_id:
                        try:
                            await cdp.send("Target.disposeBrowserContext", {"browserContextId": context_id}, timeout=2.0)
                        except Exception:
                            pass  # Chrome disposes a jar on its own once its last tab is gone
            finally:
                await cdp.close()
    except Exception as exc:
        logger.warning(f"Gtwy_Browser: frozen-tab check on {steel_client.short_host(host)} failed: {exc.__class__.__name__}: {exc}")
    return closed
