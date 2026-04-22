"""
Tests for monitoring.py — log setup, heartbeat, and webhook handler.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from monitoring import Heartbeat, WebhookAlertHandler, setup_logging


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def reset_root_logger():
    """Snapshot & restore the root logger so setup_logging tests don't
    leak handlers into other tests."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        yield
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


# --------------------------------------------------------------------------- #
# Heartbeat                                                                   #
# --------------------------------------------------------------------------- #
class TestHeartbeat:
    def test_write_creates_file_with_timestamp(self, tmp_dir):
        hb = Heartbeat(os.path.join(tmp_dir, "hb.json"))
        hb.write(status="ok", equity=12345.67)
        data = hb.read()
        assert data is not None
        assert data["status"] == "ok"
        assert data["equity"] == 12345.67
        assert "timestamp" in data

    def test_read_missing_returns_none(self, tmp_dir):
        hb = Heartbeat(os.path.join(tmp_dir, "nonexistent.json"))
        assert hb.read() is None

    def test_read_corrupt_returns_none(self, tmp_dir):
        path = os.path.join(tmp_dir, "hb.json")
        Path(path).write_text("not valid json {")
        assert Heartbeat(path).read() is None

    def test_write_overwrites(self, tmp_dir):
        hb = Heartbeat(os.path.join(tmp_dir, "hb.json"))
        hb.write(status="first")
        hb.write(status="second")
        assert hb.read()["status"] == "second"

    def test_write_handles_nonjson_values(self, tmp_dir):
        # open_positions contains a dataclass-like nested value — must not raise
        hb = Heartbeat(os.path.join(tmp_dir, "hb.json"))
        hb.write(open_positions={"AAPL": {"qty": 10, "entry_price": 150.0}})
        data = hb.read()
        assert data["open_positions"]["AAPL"]["qty"] == 10


# --------------------------------------------------------------------------- #
# Logging setup                                                               #
# --------------------------------------------------------------------------- #
class TestSetupLogging:
    def test_creates_log_directory_and_file(self, tmp_dir, reset_root_logger):
        log_dir = os.path.join(tmp_dir, "mylogs")
        setup_logging(log_dir=log_dir, log_file="test.log")

        logger = logging.getLogger("test")
        logger.info("hello from test")
        # Flush handlers so the file exists
        for h in logging.getLogger().handlers:
            h.flush()

        log_path = os.path.join(log_dir, "test.log")
        assert os.path.exists(log_path)
        contents = Path(log_path).read_text()
        assert "hello from test" in contents

    def test_installs_webhook_handler_when_url_given(
            self, tmp_dir, reset_root_logger):
        setup_logging(log_dir=tmp_dir, webhook_url="http://example.invalid/hook")
        handlers = logging.getLogger().handlers
        assert any(isinstance(h, WebhookAlertHandler) for h in handlers)

    def test_no_webhook_when_url_missing(self, tmp_dir, reset_root_logger):
        setup_logging(log_dir=tmp_dir)
        handlers = logging.getLogger().handlers
        assert not any(isinstance(h, WebhookAlertHandler) for h in handlers)


# --------------------------------------------------------------------------- #
# Webhook handler                                                             #
# --------------------------------------------------------------------------- #
class TestWebhookHandler:
    def test_emits_post_on_error_record(self):
        handler = WebhookAlertHandler("http://example.invalid/hook")
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname=__file__,
            lineno=1, msg="boom", args=(), exc_info=None,
        )
        with patch("urllib.request.urlopen") as urlopen:
            handler.emit(record)
            assert urlopen.called
            req = urlopen.call_args.args[0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["level"] == "ERROR"
            assert "boom" in body["text"]

    def test_swallows_network_errors(self):
        """A dead webhook URL must not propagate exceptions."""
        import urllib.error
        handler = WebhookAlertHandler("http://example.invalid/hook")
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname=__file__,
            lineno=1, msg="boom", args=(), exc_info=None,
        )
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("dead")):
            # Must not raise
            handler.emit(record)
