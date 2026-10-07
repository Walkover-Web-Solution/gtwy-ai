"""Extractor to extract web search data into standard annotations for all services.

Normalizes web search tools/calls, grounding metadata, and citations into a list of annotation objects:
[
    {
        "type": "url_citation",
        "title": "...",
        "url": "...",
        "query": "...",
    },
    ...
]
"""
import html
import json
import re

_ANCHOR_RE = re.compile(r'<a\b[^>]*\bhref="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _is_gemini_web_search(tool_part):
    tool_type = tool_part.get("tool_type") or tool_part.get("toolType")
    return str(getattr(tool_type, "value", tool_type) or "") == "GOOGLE_SEARCH_WEB"


def gemini_search_tool_call_queries(response):
    """Queries from Gemini server-side google search tool_call parts (one list entry per query)."""
    candidates = response.get("candidates") if isinstance(response, dict) else None
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        return []
    parts = (candidates[0].get("content") or {}).get("parts") or []
    queries = []
    for part in parts:
        tool_call = part.get("tool_call") or part.get("toolCall") if isinstance(part, dict) else None
        if isinstance(tool_call, dict) and _is_gemini_web_search(tool_call):
            queries.extend(q for q in ((tool_call.get("args") or {}).get("queries") or []) if q)
    return queries


def gemini_search_suggestion_links(response):
    """(title, url) pairs from the search_suggestions HTML in Gemini google search tool_response parts.

    Gemini 3 with server-side tool invocations returns no URLs in the tool_response itself, only a
    rendered suggestions widget whose chips link to the search for each query.
    """
    candidates = response.get("candidates") if isinstance(response, dict) else None
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        return []
    parts = (candidates[0].get("content") or {}).get("parts") or []
    links = []
    for part in parts:
        tool_response = part.get("tool_response") or part.get("toolResponse") if isinstance(part, dict) else None
        if not isinstance(tool_response, dict) or not _is_gemini_web_search(tool_response):
            continue
        suggestions = (tool_response.get("response") or {}).get("search_suggestions") or ""
        for href, text in _ANCHOR_RE.findall(suggestions if isinstance(suggestions, str) else ""):
            title = html.unescape(_TAG_RE.sub("", text)).strip()
            links.append((title, html.unescape(href)))
    return links


def gemini_web_search_count(response):
    """Number of google searches in a Gemini response, for per-search billing."""
    tool_call_queries = gemini_search_tool_call_queries(response)
    if tool_call_queries:
        return len(tool_call_queries)
    candidates = response.get("candidates") if isinstance(response, dict) else None
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        return 0
    grounding = candidates[0].get("grounding_metadata") or candidates[0].get("groundingMetadata") or {}
    return len(grounding.get("web_search_queries") or grounding.get("webSearchQueries") or [])



def anthropic_web_search_annotations(response):
    """Sources from an Anthropic web search response.

    Per the web search tool docs: each search is a ``server_tool_use`` block (input.query) followed by a
    ``web_search_tool_result`` whose content is a list of ``web_search_result`` (url, title, page_age) or,
    on failure, a single ``web_search_tool_result_error`` object (error_code). Text blocks carry
    ``web_search_result_location`` citations (url, title, cited_text). With dynamic filtering
    (web_search_20260209+) the same block types appear nested under code execution with a ``caller``
    field, and results may be omitted (response_inclusion="excluded") leaving only the citations.
    """
    blocks = [block for block in response.get("content") or [] if isinstance(block, dict)]
    queries_by_call = {
        block.get("id"): (block.get("input") or {}).get("query") or ""
        for block in blocks
        if block.get("type") in ("server_tool_use", "tool_use") and block.get("name") == "web_search"
    }

    annotations, by_url = [], {}
    for block in blocks:
        if block.get("type") != "web_search_tool_result":
            continue
        query = queries_by_call.get(block.get("tool_use_id"), "")
        content = block.get("content")
        if isinstance(content, dict):
            # Failed search: HTTP 200 with a single error object instead of a result list
            annotations.append({
                "type": "web_search_error",
                "title": "Web Search",
                "url": "",
                "query": query,
                "error_code": content.get("error_code") or "",
            })
            continue
        for result in content or []:
            if not isinstance(result, dict) or result.get("type") != "web_search_result":
                continue
            url = result.get("url") or ""
            if url in by_url:
                continue
            entry = {
                "type": "url_citation",
                "title": result.get("title") or "",
                "url": url,
                "query": query,
                "page_age": result.get("page_age") or "",
                "cited_text": [],
            }
            by_url[url] = entry
            annotations.append(entry)

    for block in blocks:
        if block.get("type") != "text":
            continue
        for citation in block.get("citations") or []:
            if not isinstance(citation, dict) or citation.get("type") != "web_search_result_location":
                continue
            url = citation.get("url") or ""
            entry = by_url.get(url)
            if entry is None:
                # Results can be excluded from the response; the citation is then the only record
                entry = {"type": "url_citation", "title": citation.get("title") or "", "url": url, "query": "", "cited_text": []}
                by_url[url] = entry
                annotations.append(entry)
            cited_text = citation.get("cited_text")
            if cited_text and cited_text not in entry["cited_text"]:
                entry["cited_text"].append(cited_text)

    for entry in annotations:
        if entry.get("cited_text") == []:
            entry.pop("cited_text")
    return annotations

def extract_web_search_annotations(response, service):
    if not isinstance(response, dict):
        return []

    annotations = []

    if service == "openai":
        # 1. Check Responses API 'output' array (used by /v1/responses)
        output_items = response.get("output", [])
        if isinstance(output_items, list):
            for item in output_items:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")

                # Web search call items
                if item_type == "web_search_call":
                    query = item.get("query") or (item.get("action") or {}).get("query")
                    # Check sources/results inside the web_search_call if present
                    sources = item.get("sources") or item.get("results") or []
                    if isinstance(sources, list) and len(sources) > 0:
                        for src in sources:
                            if isinstance(src, dict):
                                annotations.append({
                                    "type": "url_citation",
                                    "title": src.get("title") or src.get("name") or "",
                                    "url": src.get("url") or src.get("link") or src.get("uri") or "",
                                    "query": query or "",
                                })
                    else:
                        # Add search query record even if detailed individual sources aren't broken down
                        annotations.append({
                            "type": "url_citation",
                            "title": item.get("name") or "Web Search",
                            "url": item.get("url") or "",
                            "query": query or "",
                        })

                # Message content annotations (e.g. url_citation annotations inside message content)
                elif item_type in ("message", "output_text"):
                    contents = item.get("content", [])
                    if isinstance(contents, list):
                        for c in contents:
                            if isinstance(c, dict):
                                c_annotations = c.get("annotations", [])
                                if isinstance(c_annotations, list):
                                    for ann in c_annotations:
                                        if isinstance(ann, dict):
                                            annotations.append({
                                                "type": ann.get("type") or "url_citation",
                                                "title": ann.get("title") or ann.get("text") or "",
                                                "url": ann.get("url") or ann.get("link") or "",
                                                "query": ann.get("query") or "",
                                            })

        # 2. Check standard OpenAI choices shape (Chat Completions)
        choices = response.get("choices", [])
        if isinstance(choices, list) and len(choices) > 0:
            msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            msg_annotations = msg.get("annotations", [])
            if isinstance(msg_annotations, list):
                for ann in msg_annotations:
                    if isinstance(ann, dict):
                        annotations.append({
                            "type": ann.get("type") or "url_citation",
                            "title": ann.get("title") or ann.get("text") or "",
                            "url": ann.get("url") or ann.get("link") or "",
                            "query": ann.get("query") or "",
                        })

    elif service == "gemini":
        candidates = response.get("candidates", [])
        if isinstance(candidates, list) and len(candidates) > 0:
            cand = candidates[0] if isinstance(candidates[0], dict) else {}
            grounding = cand.get("groundingMetadata") or cand.get("grounding_metadata") or {}
            queries = grounding.get("webSearchQueries") or grounding.get("web_search_queries") or []
            # With server-side tool invocations, searches also arrive as tool_call parts
            tool_call_queries = gemini_search_tool_call_queries(response)
            if not queries:
                queries = tool_call_queries
            default_query = queries[0] if (isinstance(queries, list) and queries) else ""

            chunks = grounding.get("groundingChunks") or grounding.get("grounding_chunks") or []
            if isinstance(chunks, list):
                for chunk in chunks:
                    if not isinstance(chunk, dict):
                        continue
                    web = chunk.get("web", {})
                    if isinstance(web, dict) and (web.get("uri") or web.get("title")):
                        annotations.append({
                            "type": "grounding_chunk",
                            "title": web.get("title") or "",
                            "url": web.get("uri") or "",
                            "query": default_query or "",
                        })

            # No grounding sources: fall back to the search links in the tool_response suggestions
            if not annotations:
                for title, url in gemini_search_suggestion_links(response):
                    annotations.append({
                        "type": "url_citation",
                        "title": title or "Web Search",
                        "url": url,
                        "query": title or default_query or "",
                    })

            # Nothing linkable returned: still record each search query that ran
            if not annotations:
                for query in tool_call_queries:
                    annotations.append({
                        "type": "url_citation",
                        "title": "Web Search",
                        "url": "",
                        "query": query or "",
                    })

    elif service == "anthropic":
        annotations.extend(anthropic_web_search_annotations(response))

    elif service == "groq":
        # Server-side browser_search results are returned in message.executed_tools
        choices = response.get("choices", [])
        msg = choices[0].get("message", {}) if (isinstance(choices, list) and choices and isinstance(choices[0], dict)) else {}
        executed_tools = msg.get("executed_tools") or []
        if isinstance(executed_tools, list):
            for tool in executed_tools:
                if not isinstance(tool, dict):
                    continue
                try:
                    arguments = json.loads(tool.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
                query = arguments.get("query") if isinstance(arguments, dict) else ""
                results = (tool.get("search_results") or {}).get("results") or tool.get("browser_results") or []
                for result in results:
                    if isinstance(result, dict):
                        annotations.append({
                            "type": "url_citation",
                            "title": result.get("title") or "",
                            "url": result.get("url") or "",
                            "query": query or "",
                        })

    else:
        # Generic fallback for OpenAI-compatible / Grok / Deepseek / etc.
        choices = response.get("choices", [])
        if isinstance(choices, list) and len(choices) > 0:
            msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            msg_annotations = msg.get("annotations", [])
            if isinstance(msg_annotations, list):
                for ann in msg_annotations:
                    if isinstance(ann, dict):
                        annotations.append({
                            # keep extra search data such as cited_text
                            **{k: v for k, v in ann.items() if k in ("cited_text", "page_age")},
                            "type": ann.get("type") or "url_citation",
                            "title": ann.get("title") or ann.get("text") or "",
                            "url": ann.get("url") or ann.get("link") or "",
                            "query": ann.get("query") or "",
                        })

        # Moonshot built-in $web_search calls, collected by the handler across tool turns
        web_search_calls = response.get("web_search_calls", [])
        if isinstance(web_search_calls, list):
            for call in web_search_calls:
                if not isinstance(call, dict):
                    continue
                arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
                annotations.append({
                    "type": "url_citation",
                    "title": "Web Search",
                    "url": "",
                    "query": arguments.get("query") or "",
                })

    return annotations


def _json_arguments(arguments):
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_message(response):
    choices = response.get("choices") if isinstance(response, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0].get("message") or {}
    return {}


def web_search_queries(response, service):
    """Every search query the provider ran for this response, in order."""
    if not isinstance(response, dict):
        return []
    queries = []
    if service == "gemini":
        candidates = response.get("candidates") or [{}]
        cand = candidates[0] if isinstance(candidates[0], dict) else {}
        grounding = cand.get("grounding_metadata") or cand.get("groundingMetadata") or {}
        queries = list(grounding.get("web_search_queries") or grounding.get("webSearchQueries") or [])
        queries = queries or gemini_search_tool_call_queries(response)
    elif service == "anthropic":
        queries = [
            (block.get("input") or {}).get("query")
            for block in response.get("content") or []
            if isinstance(block, dict)
            and block.get("type") in ("server_tool_use", "tool_use")
            and block.get("name") == "web_search"
        ]
    elif service == "groq":
        queries = [
            _json_arguments(tool.get("arguments")).get("query")
            for tool in _first_message(response).get("executed_tools") or []
            if isinstance(tool, dict)
        ]
    else:
        # OpenAI Responses (web_search_call items) and Grok (converted from xAI Responses)
        for item in response.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "web_search_call":
                queries.append(item.get("query") or (item.get("action") or {}).get("query"))
        queries.extend(response.get("web_search_queries") or [])
        # Moonshot $web_search calls collected by the handler
        for call in response.get("web_search_calls") or []:
            if isinstance(call, dict):
                queries.append(_json_arguments(call.get("arguments")).get("query"))
    return [q for q in queries if isinstance(q, str) and q]


def web_search_count(response, service):
    """Number of web searches the provider ran for this response."""
    if not isinstance(response, dict):
        return 0
    if service == "gemini":
        return gemini_web_search_count(response)
    if service == "anthropic":
        # Billed searches as reported by the API (failed searches are not counted)
        return ((response.get("usage") or {}).get("server_tool_use") or {}).get("web_search_requests") or 0
    if service == "groq":
        return sum(1 for tool in _first_message(response).get("executed_tools") or [] if isinstance(tool, dict))
    if response.get("web_search_count"):
        return response["web_search_count"]
    if response.get("web_search_calls"):
        return len(response["web_search_calls"])
    return sum(
        1 for item in response.get("output") or [] if isinstance(item, dict) and item.get("type") == "web_search_call"
    ) or len(web_search_queries(response, service))


def enrich_annotations(annotations, response, service):
    """Give every stored annotation the same shape: type, title, url, query plus the
    message-level search data (all queries run and the number of searches).

    Duplicate sources are dropped, and when a provider reports searches but no sources, one
    entry per query is kept so the search itself is still recorded.
    """
    queries = web_search_queries(response, service)
    count = web_search_count(response, service)
    default_query = queries[0] if queries else ""

    enriched, seen = [], set()
    for annotation in annotations or []:
        if not isinstance(annotation, dict):
            continue
        url = annotation.get("url") or annotation.get("link") or annotation.get("uri") or ""
        query = annotation.get("query") or default_query
        key = (url, query) if url else None
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        enriched.append({
            **annotation,
            "type": annotation.get("type") or "url_citation",
            "title": annotation.get("title") or annotation.get("text") or url or "Web Search",
            "url": url,
            "query": query,
        })

    if any(annotation["url"] for annotation in enriched):
        # Real sources came back: drop link-less query records (the queries stay on every entry);
        # failed searches are kept so their error_code is still recorded.
        enriched = [a for a in enriched if a["url"] or a.get("type") == "web_search_error"]
    if not enriched:
        enriched = [{"type": "url_citation", "title": "Web Search", "url": "", "query": q} for q in queries]
    if not enriched:
        return annotations

    if not count and service != "anthropic":
        # Providers that do not report a count (e.g. OpenRouter's web plugin) still ran a search
        count = max(len(queries), 1)
    for annotation in enriched:
        annotation["queries"] = queries
        annotation["search_count"] = count
    return enriched
