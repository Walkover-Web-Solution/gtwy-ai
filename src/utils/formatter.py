import json
import re
import uuid

# ----------------- PATH HELPER -----------------
def set_value_by_path(obj, path, value):
    # "children[0].children[1].value" -> ["children", "0", "children", "1", "value"]
    parts = [p for p in re.split(r'\.|\[', path.replace(']', '')) if p]
    
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

# ----------------- ICON MAPPER -----------------
def get_lucide_svg(name, size_class="w-5 h-5", color_class="text-current"):
    """
    Returns an SVG string for a requested Lucide icon name.
    """
    # Common Lucide icons (paths only)
    icons = {
        "check": '<path d="M20 6 9 17l-5-5"/>',
        "x": '<path d="M18 6 6 18"/><path d="M6 6 18 18"/>',
        "user": '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
        "mail": '<rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
        "send": '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
        "arrow-right": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
        "menu": '<line x1="4" x2="20" y1="12" y2="12"/><line x1="4" x2="20" y1="6" y2="6"/><line x1="4" x2="20" y1="18" y2="18"/>',
        "alert-circle": '<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/>',
        "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
        "calendar": '<rect width="18" height="18" x="3" y="4" rx="2" ry="2"/><line x1="16" x2="16" y1="2" y2="6"/><line x1="8" x2="8" y1="2" y2="6"/><line x1="3" x2="21" y1="10" y2="10"/>',
        "chevron-down": '<path d="m6 9 6 6 6-6"/>',
        "zap": '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>'
    }
    
    content = icons.get(name, icons["info"]) # Fallback to info
    
    return f'''
    <svg xmlns="http://www.w3.org/2000/svg" 
         viewBox="0 0 24 24" 
         fill="none" 
         stroke="currentColor" 
         stroke-width="2" 
         stroke-linecap="round" 
         stroke-linejoin="round" 
         class="{size_class} {color_class}">
      {content}
    </svg>
    '''

# ----------------- RENDERER -----------------
def render_node(node):
    if not node or not isinstance(node, dict):
        return ""

    node_type = node.get("type")

    # CARD WRAPPER
    if node_type == "Card":
        padding = node.get("padding")
        padding_class = "p-6"
        if isinstance(padding, (int, float)):
            padding_class = f"p-{padding}"

        size = node.get("size")
        if size == "sm": padding_class = "p-4"
        elif size == "lg": padding_class = "p-8"

        # Handle background (gradient or color)
        background = node.get("background", "")
        bg_style = ""
        bg_class = "bg-base-100"
        
        if background:
            if background.startswith("linear-gradient") or background.startswith("radial-gradient"):
                bg_style = f'style="background: {background};"'
                bg_class = ""  # Remove default bg class when using gradient
            else:
                # Handle solid colors or other CSS background values
                bg_style = f'style="background: {background};"'
                bg_class = ""

        # Handle theme for text color
        theme = node.get("theme", "")
        text_class = ""
        if theme == "dark":
            text_class = "text-white"
        elif theme == "light":
            text_class = "text-gray-900"

        children = node.get("children", [])
        body = "\n".join([render_node(child) for child in children])

        actions_html = ""
        confirm = node.get("confirm")
        cancel = node.get("cancel")

        if confirm or cancel:
            buttons = []
            if cancel and cancel.get("label"):
                buttons.append(render_node({
                    "type": "Button",
                    "label": cancel.get("label"),
                    "variant": "ghost",
                    "submit": False
                }))
            if confirm and confirm.get("label"):
                buttons.append(render_node({
                    "type": "Button",
                    "label": confirm.get("label"),
                    "variant": "primary",
                    "submit": True
                }))
            actions_html = f"""
            <div class="card-actions justify-end mt-4">
              {' '.join(buttons)}
            </div>
            """

        return f"""
        <div class="card {bg_class} {text_class} shadow-xl border border-base-200 w-full max-w-xl mx-auto text-center" {bg_style}>
          <div class="card-body {padding_class} items-center">
            {body}
            {actions_html}
          </div>
        </div>
        """.strip()

    elif node_type == "Form":
        children = node.get("children", [])
        rendered_children = "\n".join([render_node(child) for child in children])
        return f"""
        <form class="flex flex-col gap-4 w-full">
          {rendered_children}
        </form>
        """.strip()

    elif node_type == "DatePicker":
        name_attr = f' name="{node.get("name")}"' if node.get("name") else ""
        return f"""
        <div class="form-control w-full">
          <label class="label cursor-pointer justify-start gap-2 border border-base-300 rounded-lg px-3 py-2 bg-base-100">
            {get_lucide_svg("calendar", "w-4 h-4", "text-base-content/70")}
            <input type="date" class="bg-transparent outline-none text-sm w-full" {name_attr} />
          </label>
        </div>
        """.strip()

    elif node_type == "Col":
        gap = f"gap-{node.get('gap', 3)}"
        align_map = {
            "center": "items-center",
            "start": "items-start",
            "end": "items-end"
        }
        align_class = align_map.get(node.get("align"), "items-stretch")
        children = node.get("children", [])
        rendered_children = "\n".join([render_node(child) for child in children])
        return f"""
        <div class="flex flex-col {gap} {align_class} w-full">
          {rendered_children}
        </div>
        """.strip()

    elif node_type == "Row":
        gap = f"gap-{node.get('gap', 2)}"
        align_map = {
            "center": "items-center",
            "start": "items-start",
            "end": "items-end"
        }
        align_class = align_map.get(node.get("align"), "items-center")
        justify_map = {
            "center": "justify-center",
            "start": "justify-start",
            "end": "justify-end",
            "between": "justify-between"
        }
        justify_class = justify_map.get(node.get("justify"), "justify-center")

        children = node.get("children", [])
        rendered_children = "\n".join([render_node(child) for child in children])

        return f"""
        <div class="flex flex-row {gap} {align_class} {justify_class} w-full">
          {rendered_children}
        </div>
        """.strip()

    elif node_type == "Spacer":
        return '<div class="flex-1"></div>'

    elif node_type == "Divider":
        return '<div class="border-t border-base-300 my-2 w-full"></div>'

    elif node_type == "Box":
        bg_class = node.get("background", "")
        if bg_class == "green-400": bg_class = "bg-success text-success-content"
        elif bg_class == "red-400": bg_class = "bg-error text-error-content"
        elif bg_class == "blue-400": bg_class = "bg-info text-info-content"

        radius = node.get("radius", "md")
        rounded_class = "rounded-xl"
        if radius == "full": rounded_class = "rounded-full"

        padding = node.get("padding", 2)
        p_class = f"p-{padding}"

        flex = node.get("flex")
        width = node.get("width")

        # Improved flex handling for better responsiveness
        flex_class = ""
        width_style = ""
        if flex:
            flex_class = f"flex-[{flex}_1_auto] min-w-0"
        elif width:
            width_style = f"style='min-width:{width}px;flex:0 0 auto;'"
        else:
            flex_class = "flex-1 min-w-0"

        children = node.get("children", [])
        rendered_children = "\n".join([render_node(child) for child in children])

        return f"""
        <div class="{bg_class} {rounded_class} {p_class} flex items-center {flex_class}"
             {width_style}>
          {rendered_children}
        </div>
        """.strip()

    elif node_type == "Image":
        src = node.get('src', '')
        size = node.get("size", 48)
        alt = node.get('alt', 'image')
        return f"""
        <div class="avatar">
          <div class="w-{int(size/4)} rounded-xl">
            <img src="{src}" alt="{alt}" />
          </div>
        </div>
        """.strip()

    elif node_type == "Title":
        return f"""
        <h2 class="card-title text-base-content text-xl md:text-2xl">
          {node.get("value", "")}
        </h2>
        """.strip()

    elif node_type == "Caption":
        color_map = {
            "emphasis": "text-base-content font-semibold",
            "secondary": "text-base-content/70"
        }
        color_class = color_map.get(node.get("color"), "text-base-content/70")
        return f"""
        <span class="text-xs uppercase tracking-wide break-words {color_class}">
          {node.get("value", "")}
        </span>
        """.strip()

    elif node_type == "Text":
        size_map = {"xs": "text-xs", "sm": "text-sm", "lg": "text-lg"}
        size_class = size_map.get(node.get("size"), "text-sm")

        color_map = {
            "secondary": "text-base-content/70",
            "tertiary": "text-base-content/50",
            "success": "text-success",
            "error": "text-error"
        }
        color_class = color_map.get(node.get("color"), "text-base-content")

        base_classes = f"{size_class} {color_class}"
        if node.get("weight") == "semibold":
            base_classes += " font-semibold"

        if node.get("editable"):
            editable = node["editable"]
            name = editable.get("name", "")
            placeholder = editable.get("placeholder", "")
            value = node.get("value", "")
            if node.get("minLines", 0) > 1:
                return f"""
                <textarea class="textarea textarea-bordered w-full {base_classes}"
                          name="{name}"
                          placeholder="{placeholder}"
                          rows="{node.get('minLines')}">{value}</textarea>
                """.strip()
            return f"""
            <input type="text"
                   class="input input-sm input-ghost w-full {base_classes} px-0"
                   name="{name}"
                   value="{value}"
                   placeholder="{placeholder}" />
            """.strip()

        return f'<p class="{base_classes} break-words overflow-hidden text-ellipsis">{node.get("value", "")}</p>'

    elif node_type == "Input":
        input_type = node.get("inputType", "text")
        placeholder = node.get("placeholder", "")
        name = node.get("name", "")
        return f"""
        <input type="{input_type}" name="{name}" placeholder="{placeholder}" class="input input-bordered w-full" />
        """.strip()

    elif node_type == "Icon":
        name = node.get("name")
        color = node.get("color", "text-base-content")
        size = node.get("size", "md")

        if color == "white": color_class = "text-white"
        elif color == "success": color_class = "text-success"
        elif color == "danger": color_class = "text-error"
        else: color_class = "text-current"

        size_class = "w-5 h-5"
        if size == "sm": size_class = "w-4 h-4"
        elif size == "lg": size_class = "w-6 h-6"
        elif size == "xl": size_class = "w-8 h-8"
        elif size == "3xl": size_class = "w-10 h-10"

        return get_lucide_svg(name, size_class, color_class)

    elif node_type == "Button":
        label = node.get("label", "")
        variant = node.get("variant", "neutral")

        btn_class = "btn"
        if variant == "primary": btn_class += " btn-primary"
        elif variant == "secondary": btn_class += " btn-secondary"
        elif variant == "outline": btn_class += " btn-outline"
        elif variant == "ghost": btn_class += " btn-ghost"
        elif variant == "link": btn_class += " btn-link"
        else: btn_class += " btn-neutral"

        # Handle block buttons - when in a row, use flex-1 instead of btn-block
        if node.get("block"): 
            btn_class += " flex-1"
        
        if node.get("size") == "sm": btn_class += " btn-sm"
        if node.get("size") == "lg": btn_class += " btn-lg"

        data_action_attr = ""
        btn_id = node.get("id") or f"btn-{uuid.uuid4()}"
        id_attr = f'id="{btn_id}"'

        if node.get("onClickAction"):
            try:
                action_data = json.dumps(node["onClickAction"]).replace('"', '&quot;')
                data_action_attr = f'data-action="{action_data}"'
            except:
                pass

        return f"""
        <button {id_attr} class="{btn_class}" {data_action_attr}>
          {label}
        </button>
        """.strip()

    if isinstance(node.get("children"), list):
        return "\n".join([render_node(child) for child in node["children"]])

    return ""

def render_card_to_tailwind(card_json):
    # Depending on structure, root might be "template_1" or just the object
    root = card_json.get("template_1") or card_json.get("template_2") or card_json
    return render_node(root)

def render_template_to_html(card_json_template, variables_data={}): 
    # Apply variables using dot-notation paths
    if variables_data:
        apply_variables(card_json_template.get('template_format', {}), variables_data)
    
    # Render
    return render_node(card_json_template.get('template_format', {}))
