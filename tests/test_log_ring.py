"""Tests for the support bundle's in-memory log tail, pure logic, no hass."""

from __future__ import annotations

import logging

from custom_components.emhass_companion.log_ring import (
    MAX_MESSAGE_CHARS,
    MAX_RECORDS,
    LogRingHandler,
)


def _record(message: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="custom_components.emhass_companion.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args or None,
        exc_info=None,
    )


def test_records_are_kept_oldest_first() -> None:
    handler = LogRingHandler()
    for index in range(3):
        handler.emit(_record("line %s", index))

    assert [entry["message"] for entry in handler.snapshot()] == ["line 0", "line 1", "line 2"]


def test_the_buffer_is_bounded_and_drops_the_oldest() -> None:
    handler = LogRingHandler()
    for index in range(MAX_RECORDS + 5):
        handler.emit(_record("line %s", index))

    snapshot = handler.snapshot()
    assert len(snapshot) == MAX_RECORDS
    assert snapshot[0]["message"] == "line 5"


def test_one_enormous_record_cannot_dominate_the_bundle() -> None:
    handler = LogRingHandler()
    handler.emit(_record("x" * (MAX_MESSAGE_CHARS + 100)))

    message = handler.snapshot()[0]["message"]
    assert message.startswith("x" * MAX_MESSAGE_CHARS)
    assert message.endswith("[100 more characters]")


def test_a_record_with_bad_format_args_neither_raises_nor_is_kept(
    monkeypatch,
) -> None:
    """A mismatched logging call must stay a logging problem.

    This handler sits on the package logger, so it sees every record from
    every submodule. Left unguarded, one `_LOGGER.debug("%s", a, b)` typo
    would raise out of the logging call itself -- normally in the middle of a
    coordinator run or an executor apply, which is a far worse outcome than
    the stderr line the same typo produces without this handler installed.
    """
    handler = LogRingHandler()
    handled: list[logging.LogRecord] = []
    monkeypatch.setattr(handler, "handleError", handled.append)

    handler.emit(_record("only one placeholder: %s", "a", "b"))

    assert handler.snapshot() == []
    assert len(handled) == 1
