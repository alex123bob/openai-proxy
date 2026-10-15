import pytest


@pytest.fixture(autouse=True)
def _default_body_logging(monkeypatch):
    """A developer's .env may enable body logging; tests start from the defaults."""
    monkeypatch.delenv("LOG_BODIES", raising=False)
    monkeypatch.delenv("LOG_BODY_MAX_BYTES", raising=False)
