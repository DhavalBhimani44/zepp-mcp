# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Promote spike captures into the repo's committed fixture corpus.

Two gates, because the corpus lands in a repository intended for
open-sourcing and git history is not undoable:

1. Captures carrying GPS *streams* are excluded wholesale. Scrubbing a
   delta-encoded coordinate track without destroying its structure is a
   decoder-shaped problem; excluding the file is not.
2. Everything else is re-scrubbed through the current Redactor before being
   written, so fixtures pick up SENSITIVE_KEYS entries added after capture
   time (`location`, `city`).

Run from spike/:  uv run promote.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from redact import Redactor

# Geohash charset (base32, no a/i/l/o). Zepp embeds a per-split geohash as the
# third field of each kilo_pace / mile_pace record, e.g.
#   "0,347,tek3rpwnjm,-1,140,..."
# Key-name redaction cannot reach inside a packed string, so these are matched
# positionally instead.
_SPLIT_GEOHASH = re.compile(r"(?<=,)[0-9b-hjkmnp-z]{8,12}(?=,)")
_GEO_PACKED_KEYS = ("kilo_pace", "mile_pace")

OUT = Path(__file__).parent / "out"
DEST = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "zepp"

# Fields whose presence means the capture carries a real GPS track.
GPS_STREAM_KEYS = ("longitude_latitude", "altitude", "accuracy", "DEMAltitude")


def carries_gps(body: object) -> bool:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return False
    return any(
        isinstance(data.get(key), str) and data[key].strip()
        for key in GPS_STREAM_KEYS
    )


def scrub_packed_geohashes(obj: object) -> object:
    """Replace the geohash token inside kilo_pace / mile_pace records."""
    if isinstance(obj, dict):
        return {
            key: (
                _SPLIT_GEOHASH.sub("<geohash>", value)
                if key in _GEO_PACKED_KEYS and isinstance(value, str)
                else scrub_packed_geohashes(value)
            )
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [scrub_packed_geohashes(item) for item in obj]
    return obj


def harvest_location_values(record: dict) -> set[str]:
    """Collect literal location/city values so they can be registered for
    substring replacement — that is what reaches the raw body_text copy,
    which key-name redaction cannot touch."""
    found: set[str] = set()

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in ("location", "city") and isinstance(value, str) and value.strip():
                    found.add(value.strip())
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(record.get("body_parsed"))
    return found


def main() -> None:
    if not OUT.is_dir():
        sys.exit(f"No capture corpus at {OUT}. Run probe.py first.")

    redactor = Redactor()

    # First pass: register every literal location value found anywhere, so the
    # substring mechanism scrubs them out of body_text too.
    for path in sorted(OUT.glob("*.json")):
        record = json.loads(path.read_text())
        if isinstance(record, dict):
            for value in harvest_location_values(record):
                redactor.register(value, "location")
    DEST.mkdir(parents=True, exist_ok=True)
    for stale in DEST.glob("*.json"):
        stale.unlink()

    promoted, excluded = [], []
    for path in sorted(OUT.glob("*.json")):
        record = json.loads(path.read_text())

        # index.json is a list of per-capture metadata, not a capture record.
        # It holds no response bodies, so it only needs a scrub pass.
        if not isinstance(record, dict):
            (DEST / path.name).write_text(
                json.dumps(redactor.scrub(record), indent=2, ensure_ascii=False)
            )
            promoted.append(path.name)
            continue

        if carries_gps(record.get("body_parsed")):
            excluded.append(path.name)
            continue
        # url and params are scrubbed at capture time, but with whatever
        # SENSITIVE_KEYS held then. Re-scrub so keys added later apply here too.
        record["url"] = redactor.scrub(record.get("url"))
        record["params"] = scrub_packed_geohashes(redactor.scrub(record.get("params")))

        record["body_parsed"] = scrub_packed_geohashes(
            redactor.scrub(record.get("body_parsed"))
        )
        body_text = redactor.scrub(record.get("body_text"))
        if isinstance(body_text, str):
            body_text = _SPLIT_GEOHASH.sub("<geohash>", body_text)
        record["body_text"] = body_text
        (DEST / path.name).write_text(json.dumps(record, indent=2, ensure_ascii=False))
        promoted.append(path.name)

    manifest = {
        "generated_from": "spike/probe.py",
        "promoted": len(promoted),
        "excluded_for_gps": excluded,
        "note": (
            "Re-scrubbed through Redactor at promotion time. Captures carrying "
            "GPS streams are excluded; a synthetic track is needed to test the "
            "GPS decoder."
        ),
    }
    (DEST / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))

    print(f"promoted {len(promoted)} -> {DEST}")
    print(f"excluded {len(excluded)} for GPS: {', '.join(excluded) or 'none'}")


if __name__ == "__main__":
    main()
