"""A bounded, in-memory record of this integration's own recent log lines.

Home Assistant's log is shared across every integration and is not something
a stranger reporting an issue thinks to go dig through -- by the time
diagnostics are downloaded, the debug line explaining why a plan looks the
way it does has usually already scrolled past anyway. A small handler
installed directly on this integration's own logger keeps the last few
hundred records in memory for exactly as long as the config entry is loaded,
so the support bundle can hand them over on its own.
"""

from __future__ import annotations

import collections
from datetime import UTC, datetime
import logging
from typing import Any

# Comfortably covers the reasoning behind one run (deferrable window
# derivation, end-SOC anchoring, forecast fallbacks, ...) without growing the
# bundle into something nobody wants to scroll through.
MAX_RECORDS = 200

# One record must not be able to dominate the bundle. The add-on discovery
# path logs the Supervisor's whole store entry for EMHASS at debug level,
# which carries the add-on's rendered long_description -- several kilobytes
# of README that arrives as a single message, larger than every other record
# in a fresh buffer combined. Bounding the record count alone does not help
# when one record is the problem.
MAX_MESSAGE_CHARS = 2000


class LogRingHandler(logging.Handler):
    """Keeps the last :data:`MAX_RECORDS` records emitted by one logger.

    A deque, not a list trimmed after the fact -- bounding memory is the
    whole point, and a fixed-size deque discards the oldest record on append
    for free instead of needing separate bookkeeping.
    """

    def __init__(self, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self._records: collections.deque[dict[str, Any]] = collections.deque(maxlen=MAX_RECORDS)

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if len(message) > MAX_MESSAGE_CHARS:
            dropped = len(message) - MAX_MESSAGE_CHARS
            message = f"{message[:MAX_MESSAGE_CHARS]}... [{dropped} more characters]"
        self._records.append(
            {
                "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
                "level": record.levelname,
                "message": message,
            }
        )

    def snapshot(self) -> list[dict[str, Any]]:
        """Records oldest first -- the order a maintainer reads a log in."""
        return list(self._records)
