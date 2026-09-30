import ast
import json
import logging
import pathlib

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.config.logging import JsonFormatter
from app.config.settings import normalize_database_url


@pytest.mark.parametrize(
    "given,expected",
    [
        ("postgresql://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("sqlite:///x.db", "sqlite:///x.db"),
    ],
)
def test_database_url_normalization(given, expected):
    assert normalize_database_url(given) == expected


def test_defaults_and_env_loading(monkeypatch):
    for name in ("LOCAL_STORAGE_ROOT", "PROCESSING_DELAY_SECONDS", "LOG_LEVEL", "QUEUE_POLL_INTERVAL_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    s = Settings(_env_file=None)
    assert s.database_url.startswith("postgresql+psycopg://")
    assert s.processing_delay_seconds == 3.0 and s.log_level == "INFO" and s.queue_poll_interval_seconds == 0.5
    monkeypatch.setenv("PROCESSING_DELAY_SECONDS", "0.25")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    s = Settings(_env_file=None)
    assert s.processing_delay_seconds == 0.25 and s.log_level == "DEBUG"


def test_database_url_is_required(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "kwargs",
    [{"log_level": "LOUD"}, {"simulate_failure_rate": 1.5}, {"processing_delay_seconds": -1}, {"queue_poll_interval_seconds": 0}],
)
def test_invalid_values_are_rejected(kwargs):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="sqlite:///x.db", **kwargs)


def test_json_formatter_includes_extras_and_exceptions():
    fmt = JsonFormatter({"service": "t"})
    logger = logging.getLogger("fmt-test")
    try:
        raise ValueError("boom")
    except ValueError:
        rec = logger.makeRecord("fmt-test", logging.ERROR, __file__, 1, "hello %s", ("world",), logging.sys.exc_info(), extra={"document_id": "abc"})
    out = json.loads(fmt.format(rec))
    assert out["message"] == "hello world" and out["service"] == "t" and out["document_id"] == "abc"
    assert out["level"] == "ERROR" and "ValueError: boom" in out["exc_info"]


def test_no_logging_extra_key_collides_with_logrecord_attributes():
    """`extra={'filename': ...}` raises KeyError at runtime; make sure no call site does that."""
    reserved = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
    offenders = []
    for path in pathlib.Path("app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.keyword) and node.arg == "extra" and isinstance(node.value, ast.Dict):
                offenders += [(str(path), k.value) for k in node.value.keys if isinstance(k, ast.Constant) and k.value in reserved]
    assert offenders == []
