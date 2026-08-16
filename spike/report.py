# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Summarise the capture corpus into FINDINGS.md."""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "out"


def main() -> None:
    index = json.loads((OUT / "index.json").read_text())

    ok = [e for e in index if e["status"] == 200 and not e["empty_200"]]
    empty = [e for e in index if e["empty_200"]]
    failed = [e for e in index if e["status"] not in (200, 303)]

    lines = [
        "# Zepp Recon Spike — Findings",
        "",
        f"**Date:** 2026-08-15 · **Captures:** {len(index)}",
        f"**With data:** {len(ok)} · **Empty-200:** {len(empty)} · "
        f"**Failed:** {len(failed)}",
        "",
        "## Endpoints returning data",
        "",
        "| Status | Bytes | Name |",
        "|---|---|---|",
    ]
    lines += [f"| {e['status']} | {e['body_bytes']} | `{e['name']}` |" for e in ok]

    lines += ["", "## Empty-200 (present but no records)", ""]
    lines += [f"- `{e['name']}`" for e in empty] or ["- none"]

    lines += ["", "## Failed", "", "| Status | Name |", "|---|---|"]
    lines += [f"| {e['status']} | `{e['name']}` |" for e in failed] or ["| — | none |"]

    lines += [
        "", "## Answers to spec section 11", "",
        "Fill each in from the captures above before writing Plan 2.", "",
        "1. **Region host:** ",
        "2. **Earliest data served:** ",
        "3. **Password flow works (Aug 2026):** ",
        "4. **Metrics this watch produces:** ",
        "5. **query_type summary vs detail:** ",
        "6. **history.json covers all sports:** ",
        "7. **Detail endpoint / TrackID format:** ",
        "8. **Index carries sport-specific summaries:** ",
        "9. **VO2 max global or per-discipline:** ",
        "10. **Multisport activities present:** ",
        "11. **Devices paired / stream collision:** ",
        "12. **Event data/extra payload shape:** ",
        "",
    ]

    path = Path(__file__).parent / "FINDINGS.md"
    path.write_text("\n".join(lines))
    print(f"Wrote {path} — {len(ok)} endpoints with data")


if __name__ == "__main__":
    main()
