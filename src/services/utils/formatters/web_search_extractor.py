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

    elif service == "anthropic":
        content_blocks = response.get("content", [])
        if isinstance(content_blocks, list):
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") in ("web_search", "web_search_20250305"):
                    input_data = block.get("input", {})
                    query = input_data.get("query") if isinstance(input_data, dict) else ""
                    annotations.append({
                        "type": "url_citation",
                        "title": "Web Search",
                        "url": "",
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
                            "type": ann.get("type") or "url_citation",
                            "title": ann.get("title") or ann.get("text") or "",
                            "url": ann.get("url") or ann.get("link") or "",
                            "query": ann.get("query") or "",
                        })

    return annotations
