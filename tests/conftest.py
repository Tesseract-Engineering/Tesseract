"""Shared fixtures. Unit tests never touch Neo4j or a model: settings point at a
temporary state folder, and model keys are cleared."""

import os

import pytest

for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "LLM_PROVIDER"):
    os.environ.pop(key, None)


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Fresh settings per test, with state and outbox under tmp_path."""
    from tesseract import config

    monkeypatch.setenv("TESSERACT_STATE", str(tmp_path / "state"))
    monkeypatch.delenv("TESSERACT_COMPANY", raising=False)
    monkeypatch.delenv("TESSERACT_OUTBOX", raising=False)
    monkeypatch.delenv("TESSERACT_DATA", raising=False)
    config.settings.cache_clear()
    yield
    config.settings.cache_clear()


@pytest.fixture
def company_file(tmp_path, monkeypatch):
    """Write a company file, point TESSERACT_COMPANY at it, and return a writer."""
    import json

    from tesseract import config

    def write(data: dict):
        path = tmp_path / "company.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setenv("TESSERACT_COMPANY", str(path))
        config.settings.cache_clear()
        return path

    return write
