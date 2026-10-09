"""Gtwy_File_Reader: lets the model read an attached file it could not take natively.

Registered by the file bridge when an attachment was cut short or an earlier
attachment was not converted, or when the agent lists it in built_in_tools.
Only URLs attached in this thread can be opened.
"""

import math

from src.configs.constant import inbuild_tools
from src.services.file_bridge import helper_models
from src.services.file_bridge.detect import IMAGE, VIDEO, detect_kind, file_name
from src.services.file_bridge.download import download_file
from src.services.utils.logger import logger


def _error(message):
    return {"response": {"error": message}, "metadata": {"type": "function"}, "status": 0}


def build_file_reader_tool_schema():
    return {
        "type": "function",
        "name": inbuild_tools["Gtwy_File_Reader"],
        "description": (
            "Read a file the user attached in this conversation (PDF, CSV, Excel, Word, PowerPoint, text, "
            "image, audio or video). Returns the file's text. Long files come in parts: start with part 1 "
            "and ask for the next part if you need more. For an image or video you can also ask a specific question."
        ),
        "properties": {
            "url": {
                "description": "The exact URL of the attached file, as shown in the conversation.",
                "type": "string",
                "enum": [],
                "required": [],
                "parameter": {},
            },
            "part": {
                "description": "Which part of a long file to return, starting at 1. Defaults to 1.",
                "type": "integer",
                "enum": [],
                "required": [],
                "parameter": {},
            },
            "question": {
                "description": "Optional. For an image or video: a specific question to answer about it.",
                "type": "string",
                "enum": [],
                "required": [],
                "parameter": {},
            },
        },
        "required": ["url"],
    }


async def _answer_about_media(url, kind, question, ctx):
    data, content_type = await download_file(url)
    from src.services.file_bridge.service import image_for_gemini

    if kind == IMAGE:
        data, mime = image_for_gemini(data, content_type)
    else:
        mime = (content_type or "").split(";")[0].strip() or "video/mp4"
    return await helper_models.gemini_read(data, mime, question, url, ctx)


async def call_file_reader(args, ctx):
    from src.services.file_bridge.service import convert_url, part_size

    args = args if isinstance(args, dict) else {}
    url = args.get("url")
    if not url:
        return _error("url is required")
    if ctx is None or url not in ctx.allowed_urls:
        return _error("this URL is not a file attached in this conversation")

    kind = ctx.url_kinds.get(url) or detect_kind(url)
    question = (args.get("question") or "").strip()
    try:
        if question and kind in (IMAGE, VIDEO):
            answer = await _answer_about_media(url, kind, question, ctx)
            return {
                "response": {"name": file_name(url), "type": kind, "answer": answer},
                "metadata": {"type": "function"},
                "status": 1,
            }

        result = await convert_url(url, kind, ctx)
        if result.get("error"):
            return _error(f"could not read {file_name(url)}: {result['error']}")
        text = result.get("text") or ""
        size = part_size()
        total_parts = max(math.ceil(len(text) / size), 1)
        try:
            part = int(args.get("part") or 1)
        except (TypeError, ValueError):
            part = 1
        if part < 1 or part > total_parts:
            return _error(f"part must be between 1 and {total_parts}")
        return {
            "response": {
                "name": file_name(url),
                "type": result.get("kind") or kind,
                "part": part,
                "total_parts": total_parts,
                "text": text[(part - 1) * size : part * size],
            },
            "metadata": {"type": "function"},
            "status": 1,
        }
    except Exception as exc:
        logger.error(f"[file_bridge] read_file failed for {file_name(url)}: {exc}")
        return _error(f"could not read {file_name(url)}: {exc}")
