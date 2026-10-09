import pytest

from src.services.file_bridge import capabilities
from src.services.file_bridge.detect import (
    AUDIO,
    CSV,
    DOCX,
    IMAGE,
    LEGACY_OFFICE,
    PDF,
    TEXT,
    UNKNOWN,
    VIDEO,
    detect_kind,
    file_name,
)


@pytest.mark.parametrize(
    ("url", "declared", "content_type", "expected"),
    [
        ("https://x.io/a/report.csv?sig=abc", None, None, CSV),
        ("https://x.io/a/Photo.JPG", None, None, IMAGE),
        ("https://x.io/a/talk.mp3", "pdf", None, AUDIO),  # history tags every file "pdf"
        ("https://x.io/a/notes.docx", "pdf", None, DOCX),
        ("https://x.io/a/clip.mp4", None, None, VIDEO),
        ("https://x.io/a/old.doc", None, None, LEGACY_OFFICE),
        ("https://x.io/a/data.json", None, None, TEXT),
        ("https://x.io/blob/123", "image", None, IMAGE),
        ("https://x.io/blob/123", "pdf", "text/csv; charset=utf-8", CSV),
        ("https://x.io/blob/123", None, "application/pdf", PDF),
        ("https://x.io/blob/123", None, None, UNKNOWN),
    ],
)
def test_detect_kind(url, declared, content_type, expected):
    assert detect_kind(url, declared, content_type) == expected


def test_file_name_decodes_path():
    assert file_name("https://x.io/files/Q3%20sales.xlsx?token=1") == "Q3 sales.xlsx"


@pytest.fixture
def models(monkeypatch):
    from src.configs import model_configuration

    document = {
        "groq": {"llama": {"validationConfig": {"vision": False, "tools": True, "context_window": 8000}}},
        "openai": {
            "gpt": {"validationConfig": {"vision": True, "files": True}},
            "gpt-nofiles": {"validationConfig": {"vision": True, "files": False}},
            "gpt-noflags": {"validationConfig": {}},
        },
        "anthropic": {"claude": {"validationConfig": {"vision": True, "tools": False}}},
        "gemini": {"flash": {"validationConfig": {"vision": True, "files": True}}},
        "open_router": {"any": {"validationConfig": {"vision": True}}},
    }
    monkeypatch.setattr(model_configuration, "model_config_document", document)
    monkeypatch.setattr(capabilities, "uses_openai_sdk", lambda service: service == "open_router")
    return document


def test_native_kinds_respect_handler_and_flags(models):
    assert capabilities.native_kinds("groq", "llama") == set()
    assert capabilities.native_kinds("openai", "gpt") == {IMAGE, PDF}
    assert capabilities.native_kinds("openai", "gpt-nofiles") == {IMAGE}
    # A missing flag trusts the handler.
    assert capabilities.native_kinds("openai", "gpt-noflags") == {IMAGE, PDF}
    # Gemini's current-turn code ignores documents even though the model reads them.
    assert capabilities.native_kinds("gemini", "flash") == {IMAGE, AUDIO}
    assert capabilities.native_kinds("open_router", "any") == {IMAGE}
    assert capabilities.native_kinds("unknown_service", "x") == set()


def test_history_kinds_differ_from_current_turn(models):
    assert capabilities.native_history_kinds("gemini", "flash") == {IMAGE, PDF, AUDIO, VIDEO}
    # The OpenAI-compatible history builders send no attachments at all.
    assert capabilities.native_history_kinds("open_router", "any") == set()


def test_tools_and_context_window(models):
    assert capabilities.supports_tools("groq", "llama") is True
    assert capabilities.supports_tools("anthropic", "claude") is False
    assert capabilities.context_window("groq", "llama") == 8000
    assert capabilities.context_window("openai", "gpt") is None
