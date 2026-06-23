import json
import re
import uuid
import ast
from typing import Any

# ----------------- PATH HELPER -----------------
def set_value_by_path(obj, path, value):
    # "children[0].children[1].value" -> ["children", "0", "children", "1", "value"]
    parts = [p for p in re.split(r'\.|\\[', path.replace(']', '')) if p]
    
    current = obj
    for i in range(len(parts) - 1):
        key = parts[i]
        
        # Handle array indices
        if isinstance(current, list):
            try:
                key = int(key)
            except ValueError:
                pass
                
        if isinstance(current, dict):
            if key not in current:
                return
            current = current[key]
        elif isinstance(current, list):
            if isinstance(key, int) and 0 <= key < len(current):
                current = current[key]
            else:
                return
        else:
            return

    final_key = parts[-1]
    
    if isinstance(current, list):
        try:
            final_key = int(final_key)
            if 0 <= final_key < len(current):
                current[final_key] = value
        except ValueError:
            pass
    elif isinstance(current, dict):
        current[final_key] = value

def apply_variables(card_json, variables):
    if not variables:
        return card_json
        
    if isinstance(variables, str):
        try:
            variables_dict = json.loads(variables)
            # Support both wrapped "item" and direct object
            data = variables_dict.get('item', variables_dict)
            if isinstance(data, dict):
                for path, value in data.items():
                    set_value_by_path(card_json, path, value)
        except json.JSONDecodeError:
            pass
    elif isinstance(variables, dict):
        for path, value in variables.items():
            set_value_by_path(card_json, path, value)
            
    return card_json

# ----------------- FLAT-KEY + INDEXED VARIABLE SUBSTITUTION -----------------
#
# Handles both:
#   Simple:  {{total}}           -> ai_data["total"]
#   Indexed: {{items[2].name}}   -> ai_data["items"][2]["name"]
#   Object:  {{source.image}}    -> ai_data["source"]["image"]
#   Alias:   {{row.route}}       -> item_context["row"]["route"]  (inside ListView loop)

_SIMPLE_FULL_RE  = re.compile(r'^\s*\{\{([a-zA-Z_]\w*)\}\}\s*$')
_INDEXED_FULL_RE = re.compile(r'^\s*\{\{([a-zA-Z_]\w*)\[(\d+)\]\.([a-zA-Z_]\w*)\}\}\s*$')
_OBJECT_FULL_RE  = re.compile(r'^\s*\{\{([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)+)\}\}\s*$')
_INLINE_RE       = re.compile(r'\{\{([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*(?:\[\d+\](?:\.[a-zA-Z_]\w*)*)?)\}\}')

# Matches a binding expressed as a single placeholder: "{{trips}}" or "{{row.buttons}}"
_BINDING_PLACEHOLDER_RE = re.compile(r'^\s*\{\{([\w][\w.]*)\}\}\s*$')


def _resolve_placeholder(root_key, index_str, field, data):
    """Return the concrete value for one placeholder."""
    if index_str is not None:
        idx = int(index_str)
        arr = data.get(root_key) or []
        if isinstance(arr, list) and idx < len(arr):
            item = arr[idx]
            return item.get(field, "") if isinstance(item, dict) else str(item)
        return ""
    if field is not None:
        obj = data.get(root_key) or {}
        if isinstance(obj, dict):
            return obj.get(field, "")
        return ""
    v = data.get(root_key)
    if v is not None:
        return v
    return f"{{{{{root_key}}}}}"


def _get_path_value(data, path):
    """
    Resolve a dot/bracket path like 'row.image' or 'trips[0].id' from a dict.
    Returns None if the path does not exist.
    """
    if not data or not path:
        return None
    parts = re.split(r'\.|\[', path.replace(']', ''))
    val = data
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part)
        elif isinstance(val, list):
            try:
                val = val[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return val


def apply_variables_to_template_json(template_format, ai_data=None):
    """
    Walk a template_format JSON tree, substitute {{placeholder}} values,
    and handle ListView loop expansion.

    Supports three ListView binding styles:
      A) New generic:  node has "itemTemplate" + optional "itemAlias"
      B) Legacy alias: node.children[0] is the item template,
                       alias from node["itemAlias"], node["key"],
                       or children[0]["key"]  (e.g. "row")
      C) No ListView:  plain recursive string substitution.

    Binding value may be:
      - A placeholder:  "{{trips}}"  → unwrapped to "trips"  then looked up in context
      - A direct key:   "rows"       → looked up directly in context
    """
    import copy

    ai_data = ai_data or {}

    def _replace_str(s, context):
        """Substitute all {{...}} placeholders in string s using context dict."""
        stripped = s.strip()

        # Full indexed: "{{items[0].name}}" → single raw value
        m = _INDEXED_FULL_RE.match(stripped)
        if m:
            return _resolve_placeholder(m.group(1), m.group(2), m.group(3), context)

        # Full object/alias dot-path: "{{row.image}}" or "{{footerButton.actionData.type}}"
        m = _OBJECT_FULL_RE.match(stripped)
        if m:
            full_path = m.group(1)
            resolved = _get_path_value(context, full_path)
            return resolved if resolved is not None else s

        # Full simple: "{{total}}" → single raw value
        m = _SIMPLE_FULL_RE.match(stripped)
        if m:
            return _resolve_placeholder(m.group(1), None, None, context)

        # Inline mixed: "From: {{row.source}} — {{footerButton.cancelActionData.data.id}}"
        def _inline_sub(match):
            full_path = match.group(0)[2:-2]  # strip {{ and }}
            resolved = _get_path_value(context, full_path)
            if resolved is not None:
                return str(resolved)
            # Fallback: simple root key lookup
            root_key = full_path.split('.')[0].split('[')[0]
            v = context.get(root_key)
            if v is not None:
                return str(v)
            return match.group(0)  # leave unreplaced

        return _INLINE_RE.sub(_inline_sub, s)

    def _replace(node, context):
        """Recursively walk node, expanding ListViews and substituting placeholders."""
        if isinstance(node, str):
            return _replace_str(node, context)

        if isinstance(node, list):
            return [_replace(item, context) for item in node]

        if isinstance(node, dict):
            # ── ListView with binding → expand data array into resolved children ──
            if node.get("type") == "ListView" and node.get("binding") is not None:
                raw_binding = node["binding"]

                # Unwrap "{{trips}}" → "trips", or use "rows" directly
                bp_match = _BINDING_PLACEHOLDER_RE.match(str(raw_binding))
                binding_key = bp_match.group(1) if bp_match else str(raw_binding)

                # Try direct key first, then dot-path resolution (e.g. "row.buttons")
                list_data = context.get(binding_key)
                if list_data is None:
                    list_data = _get_path_value(context, binding_key)

                if isinstance(list_data, list):
                    # Sibling scope: non-array keys in context (e.g. buttons, headers)
                    sibling_scope = {k: v for k, v in context.items() if k != binding_key}

                    # ── Mode A: new generic (itemTemplate + itemAlias) ─────────
                    if node.get("itemTemplate"):
                        alias = node.get("itemAlias") or node.get("key") or "item"
                        resolved_children = []
                        for item in list_data:
                            item_ctx = {**context, **sibling_scope, alias: item}
                            resolved_children.append(_replace(node["itemTemplate"], item_ctx))
                        # Strip template-only keys; return a pre-resolved ListView
                        result = {k: v for k, v in node.items()
                                  if k not in ("itemTemplate", "binding", "itemAlias", "idField")}
                        result["children"] = resolved_children
                        return result

                    # ── Mode B: legacy (children[0] as item template) ─────────
                    elif node.get("children"):
                        item_template = node["children"][0]
                        alias = (node.get("itemAlias")
                                 or node.get("key")
                                 or item_template.get("key")
                                 or "item")
                        resolved_children = []
                        for item in list_data:
                            if isinstance(item, dict):
                                item_ctx = {**context, **sibling_scope, **item, alias: item}
                            else:
                                item_ctx = {**context, **sibling_scope, alias: item}
                            resolved_children.append(_replace(item_template, item_ctx))
                        result = dict(node)
                        result["children"] = resolved_children
                        return result

            # ── Normal dict: recurse into all values ──────────────────────────
            return {k: _replace(v, context) for k, v in node.items()}

        return node

    return _replace(copy.deepcopy(template_format), ai_data)

