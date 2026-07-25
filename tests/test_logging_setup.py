"""Tests for the rotating file-logging setup."""

import logging
import logging.handlers

from bot import config, logging_setup


def _snapshot_root():
    root = logging.getLogger()
    return root.handlers[:], root.level


def _restore_root(handlers, level):
    root = logging.getLogger()
    for h in root.handlers:
        try:
            h.close()
        except Exception:
            pass
    root.handlers = handlers
    root.level = level


def test_setup_logging_creates_dir_and_file_handler(tmp_path, monkeypatch):
    handlers, level = _snapshot_root()
    try:
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setattr(logging_setup, "_configured", False)
        logging.getLogger().handlers = []

        logging_setup.setup_logging()

        assert (tmp_path / "logs").is_dir()  # log directory created
        root = logging.getLogger()
        assert any(
            isinstance(h, logging.handlers.TimedRotatingFileHandler) for h in root.handlers
        )
        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    finally:
        monkeypatch.setattr(logging_setup, "_configured", True)
        _restore_root(handlers, level)


def test_setup_logging_is_idempotent(tmp_path, monkeypatch):
    handlers, level = _snapshot_root()
    try:
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setattr(logging_setup, "_configured", False)
        logging.getLogger().handlers = []

        logging_setup.setup_logging()
        count = len(logging.getLogger().handlers)
        logging_setup.setup_logging()  # second call must not add handlers again
        assert len(logging.getLogger().handlers) == count
    finally:
        monkeypatch.setattr(logging_setup, "_configured", True)
        _restore_root(handlers, level)


def test_setup_logging_survives_unwritable_dir(tmp_path, monkeypatch):
    # Point at a path that can't be created (a file where a dir is expected)
    # -> file logging is skipped, console logging still works, no crash.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    handlers, level = _snapshot_root()
    try:
        monkeypatch.setattr(config, "LOG_DIR", str(blocker / "logs"))
        monkeypatch.setattr(logging_setup, "_configured", False)
        logging.getLogger().handlers = []

        logging_setup.setup_logging()  # must not raise

        root = logging.getLogger()
        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
        assert not any(
            isinstance(h, logging.handlers.TimedRotatingFileHandler) for h in root.handlers
        )
    finally:
        monkeypatch.setattr(logging_setup, "_configured", True)
        _restore_root(handlers, level)
