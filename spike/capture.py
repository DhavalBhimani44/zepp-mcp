"""Write every probe response to disk, redacted, with an index."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from redact import Redactor


def _looks_empty(parsed: Any) -> bool:
    """An empty-200: HTTP success carrying no actual records."""
    if parsed is None:
        return False
    if isinstance(parsed, dict):
        for key in ("data", "items", "value", "result"):
            if key in parsed:
                return not parsed[key]
        return not parsed
    return not parsed


class Capture:
    def __init__(self, out_dir: Path, redactor: Redactor) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self.entries: list[dict[str, Any]] = []

    def record(
        self,
        name: str,
        method: str,
        url: str,
        params: dict | None,
        status: int,
        body_text: str,
        elapsed_ms: int,
    ) -> dict[str, Any]:
        try:
            parsed = json.loads(body_text)
        except (ValueError, TypeError):
            parsed = None

        empty_200 = status == 200 and _looks_empty(parsed)

        record = {
            "seq": len(self.entries),
            "name": name,
            "method": method,
            "url": self.redactor.scrub(url),
            "params": self.redactor.scrub(params),
            "status": status,
            "elapsed_ms": elapsed_ms,
            "body_bytes": len(body_text),
            "empty_200": empty_200,
            "body_parsed": self.redactor.scrub(parsed),
            "body_text": self.redactor.scrub(body_text),
        }

        path = self.out_dir / f"{len(self.entries):03d}_{name}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        self.entries.append(record)
        return record

    def write_index(self) -> Path:
        index = [
            {k: entry[k] for k in
             ("seq", "name", "url", "status", "body_bytes", "empty_200", "elapsed_ms")}
            for entry in self.entries
        ]
        path = self.out_dir / "index.json"
        path.write_text(json.dumps(index, indent=2))
        return path
