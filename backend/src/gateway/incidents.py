"""Loud incident reporting that does not depend on the audit database.

Every event is logged at CRITICAL and optionally appended as a JSON line to a local
file. Arguments and results are never included (they may contain customer data).
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger("gateway.incident")


class IncidentLog:
    def __init__(self, fallback_path: Path | None = None):
        self._path = fallback_path

    def emit(self, event: str, details: dict[str, Any]) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **details}
        _log.critical(json.dumps(record))
        if self._path is not None:
            try:
                with self._path.open("a") as f:
                    f.write(json.dumps(record) + "\n")
            except OSError:
                logging.getLogger("gateway").exception("could not write incident fallback log")
