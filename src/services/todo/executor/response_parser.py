import json

from globals import logger


def _fallback_question(text: str) -> list:
    """Build a single-question list when we have to ask the user something but
    the worker did not emit a structured `questions` array."""
    return [{"id": "q1", "question": text, "options": []}]


_JSON_DECODER = json.JSONDecoder()


def _extract_envelope_object(text: str) -> dict | None:
    """Pull the first JSON object out of `text`, tolerating a prose preamble
    and trailing prose. Walks `{` positions in order and uses
    `JSONDecoder.raw_decode` (which parses a value from a given index and
    returns the end position), so nested braces are handled correctly without
    a hand-rolled brace counter. Returns the first parse that yields a dict
    containing a `status` key — that's the executor envelope, not some
    incidental `{ randomNumber: 39 }` snippet inside a result string."""
    start = 0
    while True:
        idx = text.find("{", start)
        if idx == -1:
            return None
        try:
            obj, _end = _JSON_DECODER.raw_decode(text, idx)
        except json.JSONDecodeError:
            start = idx + 1
            continue
        if isinstance(obj, dict) and "status" in obj:
            return obj
        start = idx + 1


def parse_worker_response(content) -> dict:
    """Parse a worker's JSON reply.

    Handles three real-world emit patterns:
      1. pure JSON object
      2. ```json … ``` fences (or bare ``` … ``` fences)
      3. prose preamble before the JSON ("Here's the result: { … }") — the
         model ignores the no-prose rule fairly often, so we scan for the
         first `{ … }` value that has a `status` field and use that.

    Defensive about input shape: non-str → treated as empty with a warning.
    Whitespace-only → short-circuits to "no response" instead of producing
    a misleading `line 1 column 1 (char 0)` parse error."""
    if not isinstance(content, str):
        if content is not None:
            logger.warning(
                f"Worker response was {type(content).__name__}, not str — treating as empty. "
                f"Preview: {str(content)[:200]!r}"
            )
        content = ""

    stripped = content.strip()
    if not stripped:
        logger.warning("Worker response empty/whitespace-only — agent emitted no text.")
        return {
            "status": "waiting_for_user",
            "questions": _fallback_question(
                "The worker did not return a response. Please try again or provide more details."
            ),
        }

    raw = stripped
    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0].strip()

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "status" in parsed:
            return parsed
    except json.JSONDecodeError:
        pass

    extracted = _extract_envelope_object(raw)
    if extracted is not None:
        return extracted

    logger.warning(
        f"Worker response not valid JSON envelope. Raw content preview: {content[:500]!r}"
    )
    return {
        "status": "waiting_for_user",
        "result": None,
        "questions": _fallback_question(
            "The task encountered an error and couldn't complete. "
            "Agent response did not include a valid JSON envelope. "
            "Please review and provide guidance."
        ),
    }


def _normalize_questions(raw) -> list:
    """Coerce the worker's `questions` field into a list of
    {id, question, options} dicts. Accepts list or single string for safety."""
    if not raw:
        return []
    if isinstance(raw, str):
        return _fallback_question(raw)
    if not isinstance(raw, list):
        return []
    normalized = []
    for idx, item in enumerate(raw, start=1):
        if isinstance(item, dict) and item.get("question"):
            normalized.append({
                "id": str(item.get("id") or f"q{idx}"),
                "question": str(item["question"]),
                "options": item.get("options") if isinstance(item.get("options"), list) else [],
            })
        elif isinstance(item, str) and item.strip():
            normalized.append({"id": f"q{idx}", "question": item.strip(), "options": []})
    return normalized


def build_worker_result(parsed: dict) -> dict:
    """Normalise a parsed worker response into the shape the executor expects."""
    status = parsed.get("status") or "completed"
    if status == "needs_replan":
        status = "waiting_for_user"
    if status not in {"completed", "waiting_for_user", "failed"}:
        status = "waiting_for_user"
    questions = _normalize_questions(parsed.get("questions"))
    history = parsed.get("history") if isinstance(parsed.get("history"), dict) else None
    return {
        "success": status != "failed",
        "status": status,
        "result": parsed.get("result"),
        "questions": questions,
        "error": parsed.get("error"),
        "history": history,
    }
