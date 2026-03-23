def sanitize_openai_response_item(item):
    if not isinstance(item, dict):
        return item
    content = item.get("content")
    content_is_list = isinstance(content, list)
    top_needs_clean = "status" in item
    content_needs_clean = content_is_list and any(
        isinstance(p, dict) and "status" in p for p in content
    )
    if not top_needs_clean and not content_needs_clean:
        return item
    sanitized = {k: v for k, v in item.items() if k != "status"}
    if content_is_list:
        cleaned_content = []
        for part in content:
            if isinstance(part, dict) and "status" in part:
                cleaned_content.append({k: v for k, v in part.items() if k != "status"})
            else:
                cleaned_content.append(part)
        sanitized["content"] = cleaned_content
    return sanitized
