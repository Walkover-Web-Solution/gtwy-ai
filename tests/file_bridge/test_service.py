import copy

import pytest

from src.services.file_bridge import capabilities, helper_models, service
from src.services.file_bridge.context import FileBridgeContext
from src.services.utils.built_in_tools import file_reader

CSV_URL = "https://files.example.com/sales.csv"
PDF_URL = "https://files.example.com/report.pdf"
IMG_URL = "https://files.example.com/chart.png"
MP3_URL = "https://files.example.com/call.mp3"


@pytest.fixture
def fake_world(monkeypatch):
    """No Redis, no network, no helper models: just counters to check behaviour."""
    from src.configs import model_configuration

    monkeypatch.setattr(
        model_configuration,
        "model_config_document",
        {
            "groq": {"llama": {"validationConfig": {"vision": False, "tools": True}}},
            "openai": {"gpt": {"validationConfig": {"vision": True, "files": True, "tools": True}}},
        },
    )
    monkeypatch.setattr(capabilities, "uses_openai_sdk", lambda s: False)

    cache = {}
    calls = {"download": 0, "gemini": 0, "deepgram": 0}

    async def get_cached(url):
        return cache.get(url)

    async def get_or_convert(url, convert):
        if url not in cache:
            value = await convert()
            if not value.get("error"):
                cache[url] = value
            return value
        return cache[url]

    async def download_file(url, max_bytes=None):
        calls["download"] += 1
        if url == CSV_URL:
            return b"region,total\nEU,42\nUS,7\n", "text/csv"
        if url == IMG_URL:
            return b"png-bytes", "image/png"
        raise RuntimeError("404")

    async def gemini_read(data, mime, prompt, url, ctx):
        calls["gemini"] += 1
        ctx.usage_log.append({"service": "gemini", "key_source": "platform", "cost": 0.001, "url": url})
        return "A bar chart: EU 42, US 7."

    async def deepgram_transcribe(url, ctx):
        calls["deepgram"] += 1
        return "hello this is the call"

    monkeypatch.setattr(service, "get_cached", get_cached)
    monkeypatch.setattr(service, "get_or_convert", get_or_convert)
    monkeypatch.setattr(service, "download_file", download_file)
    monkeypatch.setattr(helper_models, "gemini_read", gemini_read)
    monkeypatch.setattr(helper_models, "deepgram_transcribe", deepgram_transcribe)
    return {"cache": cache, "calls": calls}


def _params(service_name, model, **overrides):
    params = {
        "service": service_name,
        "model": model,
        "user": "What is the EU total?",
        "images": [],
        "files": [],
        "audios": [],
        "user_urls": [],
        "configuration": {"conversation": [], "prompt": "be nice"},
        "customConfig": {"tools": []},
        "tool_id_and_name_mapping": {},
        "built_in_tools": [],
        "file_bridge_usage": [],
    }
    params.update(overrides)
    return params


async def test_groq_csv_becomes_text_and_leaves_files(fake_world):
    params = _params("groq", "llama", files=[CSV_URL], user_urls=[{"url": CSV_URL, "type": "file"}])
    out = await service.prepare(params, "groq")

    assert out["files"] == []
    assert '<attached_file name="sales.csv" type="csv"' in out["user"]
    assert "| EU | 42 |" in out["user"]
    assert out["user"].startswith("What is the EU total?")
    assert out["original_attachments"] == [{"url": CSV_URL, "type": "file"}]
    assert CSV_URL in out["file_bridge_ctx"].allowed_urls
    # The caller's params are untouched.
    assert params["files"] == [CSV_URL]
    assert params["user"] == "What is the EU total?"


async def test_openai_pdf_is_left_alone(fake_world):
    params = _params("openai", "gpt", files=[PDF_URL], user_urls=[{"url": PDF_URL, "type": "pdf"}])
    out = await service.prepare(params, "openai")

    assert out["files"] == [PDF_URL]
    assert out["user"] == "What is the EU total?"
    assert fake_world["calls"]["download"] == 0


async def test_image_on_text_model_uses_helper_and_logs_cost(fake_world):
    params = _params("groq", "llama", images=[IMG_URL], user=None)
    out = await service.prepare(params, "groq")

    assert out["images"] == []
    assert "A bar chart: EU 42, US 7." in out["user"]
    assert params["file_bridge_usage"] == [
        {"service": "gemini", "key_source": "platform", "cost": 0.001, "url": IMG_URL}
    ]


async def test_failed_conversion_gives_note_not_error(fake_world):
    broken = "https://files.example.com/missing.docx"
    params = _params("groq", "llama", files=[broken])
    out = await service.prepare(params, "groq")

    assert 'name="missing.docx" type="docx"' in out["user"]
    assert 'error="404"' in out["user"]


async def test_fallback_reuses_cache(fake_world):
    params = _params("groq", "llama", images=[IMG_URL])
    await service.prepare(params, "groq")
    await service.prepare(params, "groq")
    assert fake_world["calls"]["gemini"] == 1


async def test_history_uses_cache_and_never_mutates_caller(fake_world):
    fake_world["cache"][CSV_URL] = {"text": "| EU | 42 |", "kind": "csv"}
    history = [
        {"role": "user", "content": "here is a file", "user_urls": [{"url": CSV_URL, "type": "pdf"}]},
        {"role": "user", "content": "and audio", "user_urls": [{"url": MP3_URL, "type": "audio"}]},
        {"role": "assistant", "content": "ok"},
    ]
    original = copy.deepcopy(history)
    params = _params("groq", "llama", configuration={"conversation": history})
    out = await service.prepare(params, "groq")

    new_history = out["configuration"]["conversation"]
    assert history == original
    assert params["configuration"]["conversation"] is history
    assert new_history[0]["user_urls"] == []
    assert "| EU | 42 |" in new_history[0]["content"]
    # Not cached: a pointer to the reader tool, and the tool gets registered.
    assert f'url="{MP3_URL}"' in new_history[1]["content"]
    assert fake_world["calls"]["deepgram"] == 0
    assert any(t["name"] == "Gtwy_File_Reader" for t in out["customConfig"]["tools"])
    assert params["customConfig"] == {"tools": []}


async def test_long_file_is_cut_and_reader_tool_added(fake_world, monkeypatch):
    monkeypatch.setattr(service, "part_size", lambda: 20)
    params = _params("groq", "llama", files=[CSV_URL])
    out = await service.prepare(params, "groq")

    assert 'truncated="true"' in out["user"]
    assert "part=2" in out["user"]
    assert out["tool_id_and_name_mapping"]["Gtwy_File_Reader"]["type"] == "Gtwy_File_Reader"


async def test_opt_out_and_no_attachments(fake_world):
    opted_out = _params("groq", "llama", files=[CSV_URL], configuration={"file_bridge": False})
    assert await service.prepare(opted_out, "groq") is opted_out
    plain = _params("groq", "llama")
    assert await service.prepare(plain, "groq") is plain


async def test_reader_tool_refuses_unknown_urls_and_pages(fake_world, monkeypatch):
    monkeypatch.setattr(service, "part_size", lambda: 10)
    ctx = FileBridgeContext(allowed_urls={CSV_URL}, url_kinds={CSV_URL: "csv"})

    refused = await file_reader.call_file_reader({"url": "http://169.254.169.254/latest"}, ctx)
    assert refused["status"] == 0

    first = await file_reader.call_file_reader({"url": CSV_URL}, ctx)
    second = await file_reader.call_file_reader({"url": CSV_URL, "part": 2}, ctx)
    assert first["status"] == 1 and second["status"] == 1
    assert first["response"]["total_parts"] > 1
    assert first["response"]["text"] != second["response"]["text"]

    out_of_range = await file_reader.call_file_reader({"url": CSV_URL, "part": 99}, ctx)
    assert out_of_range["status"] == 0


def test_platform_cost_is_billed_and_org_key_cost_is_not():
    from src.services.commonServices.baseService.utils import _file_bridge_platform_cost

    parsed_data = {
        "org_id": "org1",
        "usage": {"cost_breakdown": {"total_cost": 1}},
        "file_bridge_usage": [
            {"service": "gemini", "model": "m", "key_source": "platform", "cost": 0.002},
            {"service": "deepgram", "model": "n", "key_source": "org", "cost": 0.5},
        ],
    }
    assert _file_bridge_platform_cost(parsed_data) == 0.002
    breakdown = parsed_data["usage"]["cost_breakdown"]["file_bridge"]
    assert breakdown["platform_cost"] == 0.002
    assert breakdown["cost"] == 0.502
