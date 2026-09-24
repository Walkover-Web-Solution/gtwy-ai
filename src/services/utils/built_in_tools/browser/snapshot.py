"""Turn Playwright's aria snapshot (YAML) into model-facing text with element refs.

Input looks like::

    - banner:
      - link "Amazon" [e1]
      - combobox "Search Amazon.in"
    - main:
      - heading "Today's Deals" [level=2]
      - text: Some copy
      - link "Sony WH-1000XM5":
        - /url: https://...

Every interactive node gets a ref ``eN`` appended after its name. The ref map
remembers how to find that node again with Playwright's role locators::

    {"e3": {"role": "combobox", "name": "Search Amazon.in", "nth": 0}}

``nth`` is the running count of identical (role, name) pairs in document order,
which matches ``page.get_by_role(role, name=..., exact=True).nth(n)``.
Pure functions only, so this module is unit-testable without a browser.
"""

import re

INTERACTIVE_ROLES = {
    "button",
    "link",
    "textbox",
    "searchbox",
    "combobox",
    "checkbox",
    "radio",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "option",
    "tab",
    "switch",
    "slider",
    "spinbutton",
    "listbox",
    "treeitem",
}

# - <role> "<name>"<rest>    (name optional; whole node may be wrapped in YAML quotes)
_NODE_RE = re.compile(r'^(?P<indent>\s*)- (?P<quote>[\'"]?)(?P<role>[a-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>.*)$')
_REF_RE = re.compile(r"\[(e\d+)\]")


def _unescape(name: str) -> str:
    return name.replace('\\"', '"').replace("\\\\", "\\")


def parse_aria_snapshot(yaml_text: str) -> tuple[str, dict[str, dict]]:
    """Return (annotated_text, ref_map). Non-interactive lines pass through unchanged."""
    ref_map: dict[str, dict] = {}
    seen: dict[tuple[str, str], int] = {}
    out_lines: list[str] = []
    counter = 0

    for line in (yaml_text or "").splitlines():
        match = _NODE_RE.match(line)
        if not match or match.group("role") not in INTERACTIVE_ROLES:
            out_lines.append(line)
            continue

        role = match.group("role")
        raw_name = match.group("name")
        name = _unescape(raw_name) if raw_name is not None else ""
        nth = seen.get((role, name), 0)
        seen[(role, name)] = nth + 1

        counter += 1
        ref = f"e{counter}"
        ref_map[ref] = {"role": role, "name": name, "nth": nth}

        name_part = f' "{raw_name}"' if raw_name is not None else ""
        out_lines.append(f"{match.group('indent')}- {match.group('quote')}{role}{name_part} [{ref}]{match.group('rest')}")

    return "\n".join(out_lines), ref_map


def truncate(text: str, max_chars: int, ref_map: dict[str, dict]) -> tuple[str, bool, dict[str, dict]]:
    """Cut at the last newline before ``max_chars`` and drop refs whose lines were cut."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False, ref_map
    cut = text.rfind("\n", 0, max_chars)
    kept = text[: cut if cut > 0 else max_chars]
    surviving = set(_REF_RE.findall(kept))
    return kept, True, {ref: meta for ref, meta in ref_map.items() if ref in surviving}
