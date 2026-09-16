import base64

from src.configs.constant import service_name
from src.configs.model_configuration import model_config_document
from src.configs.service_registry import image_generation_tool_config, web_search_tool_config
from src.services.utils.ai_middleware_format import Response_formatter
from src.services.utils.code_interpreter_service import build_code_interpreter_tool, process_code_interpreter_outputs
from src.services.utils.gcp_upload_service import uploadDoc
from src.services.utils.thread_file_context import compute_thread_file_context

from ..baseService.baseService import BaseService
from src.services.utils.mcp_utils import merge_server_side_mcp_into_tools
from src.services.utils.image_compression import fetch_images_as_data_urls

from ..createConversations import ConversationService
_PDF_TEXT_EDIT_HELPERS = '''
import fitz

def _span_for_rect(page, rect):
    """Return the text span whose bbox best overlaps `rect` (gives the font size
    and baseline of the text being replaced), or None."""
    best, best_area = None, 0.0
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                inter = fitz.Rect(s["bbox"]) & rect
                if not inter.is_empty and inter.get_area() > best_area:
                    best, best_area = s, inter.get_area()
    return best

def pdf_replace_text(doc, old, new, pages=None):
    """Replace every exact occurrence of `old` with `new`, per hit: same font
    size and baseline as the text at THAT hit (a 10pt field and a 5pt stamp on
    the same page each keep their own size). Removes only the matched glyphs:
    no white box, images and lines untouched, neighbouring text preserved."""
    changed = 0
    for i, page in enumerate(doc):
        if pages is not None and i not in pages:
            continue
        hits = page.search_for(old)
        if not hits:
            continue
        plan = []
        for rect in hits:
            span = _span_for_rect(page, rect)
            size = span["size"] if span else 10.5
            baseline_y = span["origin"][1] if span else rect.y1 - size * 0.22
            # shrink the redaction box so adjacent lines/characters are not clipped
            shrink = fitz.Rect(rect.x0 + 0.3, rect.y0 + rect.height * 0.18, rect.x1 - 0.3, rect.y1 - rect.height * 0.18)
            page.add_redact_annot(shrink, fill=False)
            plan.append((rect.x0, baseline_y, size))
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        for x, y, size in plan:
            page.insert_text((x, y), new, fontsize=size, fontname="helv", color=(0, 0, 0))
            changed += 1
    return changed

def pdf_fill_blank_after_label(doc, label, value, pages=None, min_size=8):
    """Find `label` (e.g. "Name:") and, only where nothing follows it on the
    same line (a genuinely blank field), insert `value` right after it on the
    label's own baseline at the label's font size. Skips labels that already
    have text after them and tiny labels inside stamps or footers (< min_size)."""
    filled = 0
    for i, page in enumerate(doc):
        if pages is not None and i not in pages:
            continue
        for inst in page.search_for(label):
            span = _span_for_rect(page, inst)
            size = span["size"] if span else 10
            if size < min_size:
                continue
            same_line = fitz.Rect(inst.x1, inst.y0, inst.x1 + 250, inst.y1)
            if page.get_textbox(same_line).strip():
                continue
            baseline_y = span["origin"][1] if span else inst.y1 - size * 0.22
            page.insert_text((inst.x1 + 4, baseline_y), value, fontsize=size, fontname="helv", color=(0, 0, 0))
            filled += 1
    return filled
'''.strip()


class OpenaiResponse(BaseService):
    async def execute(self):
        historyParams = {}
        tools = {}
        functionCallRes = {}
        if self.type == "image":
            self.customConfig["prompt"] = self.user
            openAIResponse = await self.image(self.customConfig, self.apikey, service_name["openai"])
            modelResponse = openAIResponse.get("modelResponse", {})
            if not openAIResponse.get("success"):
                await self.handle_failure(openAIResponse)
                raise ValueError(openAIResponse.get("error"))
            response = await Response_formatter(
                modelResponse, service_name["openai"], tools, self.type, self.image_data
            )
            historyParams = self.prepare_history_params(response, modelResponse, tools, None)
            historyParams["message"] = "image generated successfully"
            historyParams["type"] = "assistant"
        else:
            # Working-file rules for the thread: newest upload is the original,
            # newest assistant-generated file is the latest version, older
            # uploads/versions are superseded and not sent to the model.
            file_ctx = compute_thread_file_context(self.configuration.get("conversation"), self.files)
            await self.resolve_provider_files(extra_urls=[u for u in file_ctx.active_urls if u not in self.files])
            # The container must only ever see ONE version of a given document —
            # mounting the original alongside a later edit leaves the model unable
            # to tell which mounted file is current, and it has been seen both
            # reverting to the stale original and writing duplicate/misplaced
            # edits as a result. Prefer the latest generated version; fall back
            # to the current upload(s) only when nothing has been generated yet.
            working_urls = [file_ctx.latest_url] if file_ctx.latest_url else list(file_ctx.original_urls)

            conversation = (
                await ConversationService.createOpenAiConversation(
                    self.configuration.get("conversation"),
                    self.memory,
                    self.files,
                    self.provider_file_map,
                    skip_urls=file_ctx.superseded_urls,
                )
            ).get("messages", [])
            developer_prompt = self.configuration["prompt"]
            if "code_interpreter" in self.built_in_tools:
                developer_prompt = (
                    f"{developer_prompt}\n\n"
                    "File tasks with the python tool:\n"
                    "- When asked to create or modify a file, you MUST actually produce it by "
                    "running code. Never answer with code for the user to run themselves.\n"
                    "- Do the work in THIS response. Never announce what you are about to do and "
                    "then stop: phrases like 'please hold on', 'I will now process', or 'I'll "
                    "provide a link shortly' are forbidden, because nothing runs after your reply "
                    "ends. Call the python tool first; reply only once the output file exists.\n"
                    "- Save every output file under /mnt/data/ and, in the same run, verify it "
                    "exists (e.g. os.path.exists) and is non-empty before you finish.\n"
                    "- Each response runs in a brand-new, empty sandbox: nothing you saved in an "
                    "earlier turn is still there, and any input file is re-uploaded under a new "
                    "generic name (e.g. file-XXXX.pdf), never the name you or the user used before. "
                    "This is expected, not an error. Start every file task by running "
                    "os.listdir('/mnt/data') to see what is actually there right now — do not "
                    "assume or recall a filename from earlier in the conversation.\n"
                    "- Exactly one input document is provided per turn, and it is always the "
                    "correct, current version to work from — there is never a stale or duplicate "
                    "copy sitting alongside it. If a file you expected by an old name is missing, "
                    "that only means the sandbox reset; do NOT tell the user a file is missing and "
                    "do NOT ask which file to use or ask them to re-upload — just open the one PDF "
                    "present in /mnt/data and apply the requested change to it, in this same "
                    "response.\n"
                    "- If an import or library call fails, fix the code and run it again instead "
                    "of giving up. Prefer libraries available in the sandbox: for PDFs use pypdf "
                    "or PyMuPDF (fitz); do not use the removed PyPDF2.pdf module.\n"
                    "- In your final response, state the exact output filename(s) so they can "
                    "be retrieved. Do not paste the code unless the user asked for it.\n\n"
                    "Editing text already on a PDF page (replacing a name, filling a blank field "
                    "after a label like 'Name:', etc.): do NOT write your own search/redact/insert "
                    "coordinate math, do NOT use form-field/annotation widgets, and do NOT use "
                    "reportlab or any library that renders inserted text as a hyperlink/blue/"
                    "underlined style — it is easy to get the baseline or rect wrong and place "
                    "text in the wrong spot or the wrong style. Instead, in your first code cell, "
                    "paste this exact code verbatim to define two helpers, then call them for "
                    "every such edit:\n"
                    f"```python\n{_PDF_TEXT_EDIT_HELPERS}\n```\n"
                    "Use pdf_replace_text(doc, old, new, pages=[...]) to replace an exact existing "
                    "string (e.g. a name that already appears); each hit keeps its own font size and baseline. Use pdf_fill_blank_after_label(doc, "
                    "label, value, pages=[...]) to fill a field that is blank after its label — it "
                    "automatically skips that label wherever something is already written after it, "
                    "so it is safe to call across the whole document without hand-picking pages. "
                    "After calling them, reopen the saved file and confirm with page.get_text() that "
                    "the old value is gone (for replacements) and the new value appears only where "
                    "intended, before finishing."
                )
            developer = (
                [{"role": "developer", "content": developer_prompt}] if not self.reasoning_model else []
            )

            if self.image_data and isinstance(self.image_data, list):
                self.customConfig["input"] = developer + conversation
                # Inline the image bytes rather than the URL: OpenAI would otherwise
                # download the (possibly multi-MB) file itself and time out.
                image_content = [
                    {"type": "input_image", "image_url": data_url}
                    for data_url in await fetch_images_as_data_urls(self.image_data)
                ]
                content = [{"type": "input_text", "text": self.user}] + image_content if self.user else image_content
                self.customConfig["input"].append({"role": "user", "content": content})
            elif self.files and len(self.files) > 0:
                self.customConfig["input"] = developer + conversation
                file_content = []
                for file_url in self.files:
                    ref = self.provider_file_map.get(file_url)
                    if ref:
                        file_content.append({"type": "input_file", "file_id": ref["file_id"]})
                    else:
                        file_content.append({"type": "input_file", "file_url": file_url})
                content = [{"type": "input_text", "text": self.user}] + file_content if self.user else file_content
                self.customConfig["input"].append({"role": "user", "content": content})
            elif file_ctx.latest_url:
                # No new upload this turn: hand the model the latest edited version
                # so follow-up edits build on it (the original stays in history).
                self.customConfig["input"] = developer + conversation
                ref = self.provider_file_map.get(file_ctx.latest_url)
                latest_block = (
                    {"type": "input_file", "file_id": ref["file_id"]}
                    if ref
                    else {"type": "input_file", "file_url": file_ctx.latest_url}
                )
                note = (
                    f"Attached is the latest edited version of the document"
                    f"{' (' + file_ctx.latest_filename + ')' if file_ctx.latest_filename else ''}. "
                    "It has been re-mounted in this turn's sandbox under a new filename — run "
                    "os.listdir('/mnt/data') and use whatever PDF is actually there; do not look "
                    "for the old filename and do not ask the user which file to use. Apply the "
                    "requested change directly to this attached file now. Use the original upload "
                    "earlier in the conversation only if the user explicitly asks to start over or "
                    "undo the edits."
                )
                content = ([{"type": "input_text", "text": self.user}] if self.user else []) + [
                    {"type": "input_text", "text": note},
                    latest_block,
                ]
                self.customConfig["input"].append({"role": "user", "content": content})
            else:
                user = [{"role": "user", "content": self.user}] if self.user else []
                self.customConfig["input"] = developer + conversation + user

            self.customConfig = self.service_formatter(self.customConfig, service_name["openai"])

            if "tools" not in self.customConfig and "parallel_tool_calls" in self.customConfig:
                del self.customConfig["parallel_tool_calls"]

            if len(self.built_in_tools) > 0:
                if "tools" in model_config_document[self.service][self.model]["configuration"]:
                    if "tools" not in self.customConfig:
                        self.customConfig["tools"] = []

                    tools_to_append = []

                    if "web_search" in self.built_in_tools:
                        web_search_cfg = web_search_tool_config(service_name["openai"]) or {}
                        if self.web_search_filters and isinstance(self.web_search_filters, list):
                            web_search_tool = dict(web_search_cfg.get("filtered"))
                            web_search_tool["filters"] = {"allowed_domains": self.web_search_filters}
                        else:
                            web_search_tool = dict(web_search_cfg.get("unfiltered"))
                        tools_to_append.append(web_search_tool)

                    if "image_generation" in self.built_in_tools:
                        image_generation_tool = dict(
                            image_generation_tool_config(service_name["openai"]) or {"type": "image_generation"}
                        )
                        tools_to_append.append(image_generation_tool)

                    if "code_interpreter" in self.built_in_tools:
                        input_file_ids = [
                            ref["file_id"] for url in working_urls if (ref := self.provider_file_map.get(url))
                        ]
                        tools_to_append.append(build_code_interpreter_tool(input_file_ids))

                    self.customConfig["tools"].extend(tools_to_append)

            if self.stream_mode:
                openAIResponse = await self.stream(self.customConfig, self.apikey, service_name["openai"])
            else:
                openAIResponse = await self.chats(self.customConfig, self.apikey, service_name["openai"])
            modelResponse = openAIResponse.get("modelResponse", {})

            for item in modelResponse.get("output", []):
                if item.get("type") == "image_generation_call" and item.get("result"):
                    image_bytes = base64.b64decode(item["result"].strip())
                    gcp_url = await uploadDoc(
                        file=image_bytes,
                        folder="generated-images",
                        real_time=True,
                        content_type="image/png",
                    )
                    item["image_url"] = gcp_url
                    item["permanent_url"] = gcp_url
                    item.pop("result", None)

            if "code_interpreter" in self.built_in_tools:
                input_file_ids = [
                    ref["file_id"] for url in working_urls if (ref := self.provider_file_map.get(url))
                ]
                await process_code_interpreter_outputs(modelResponse, self.apikey, input_file_ids)

            if not openAIResponse.get("success"):
                await self.handle_failure(openAIResponse)
                raise ValueError(openAIResponse.get("error"))

            # Check for function calls — streaming returns has_tool_calls flag directly
            if self.stream_mode:
                has_function_call = openAIResponse.get("has_tool_calls", False)
            else:
                has_function_call = (
                    any(output.get("type") == "function_call" for output in modelResponse.get("output", []))
                    or any(output.get("type") == "tool_call" for output in modelResponse.get("output", []))
                    or any(
                        "function_call" in str(output)
                        for output in modelResponse.get("output", [])
                        if output.get("type") in ["reasoning", "message", "output_text"]
                    )
                )

            if has_function_call:
                functionCallRes = await self.function_call(
                    self.customConfig, service_name["openai"], openAIResponse, 0, {}
                )
                if not functionCallRes.get("success"):
                    await self.handle_failure(functionCallRes)
                    raise ValueError(functionCallRes.get("error"))
                self.update_model_response(modelResponse, functionCallRes)
                final_model_response = functionCallRes.get("modelResponse", {})
                tools = merge_server_side_mcp_into_tools(
                    service_name["openai"], final_model_response, functionCallRes.get("tools", {})
                )
                response = await Response_formatter(
                    final_model_response,
                    service_name["openai"],
                    tools,
                    self.type,
                    self.image_data,
                )
            else:
                tools = merge_server_side_mcp_into_tools(
                    service_name["openai"], modelResponse, {}
                )
                response = await Response_formatter(
                    modelResponse, service_name["openai"], tools, self.type, self.image_data
                )

            transfer_config = (
                functionCallRes.get("transfer_agent_config") if has_function_call and functionCallRes else None
            )
            historyParams = self.prepare_history_params(response, modelResponse, tools, transfer_config)

        # Add transfer_agent_config to return if transfer was detected
        result = {"success": True, "modelResponse": modelResponse, "historyParams": historyParams, "response": response}
        if functionCallRes.get("transfer_agent_config"):
            result["transfer_agent_config"] = functionCallRes["transfer_agent_config"]
        return result
