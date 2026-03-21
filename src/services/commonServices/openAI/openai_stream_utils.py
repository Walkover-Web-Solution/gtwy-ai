def sanitize_openai_response_item(item):
    if not isinstance(item, dict):
        return item
    sanitized = {k: v for k, v in item.items() if k not in {"status"}}
    if isinstance(sanitized.get("content"), list):
        cleaned_content = []
        for part in sanitized["content"]:
            if isinstance(part, dict):
                cleaned_content.append({k: v for k, v in part.items() if k not in {"status"}})
            else:
                cleaned_content.append(part)
        sanitized["content"] = cleaned_content
    return sanitized
