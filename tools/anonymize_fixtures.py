# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Strip personal identity from the fixture corpus before publication.

The recon spike's redactor targeted CREDENTIALS -- tokens, passwords, the
user id. That was the right goal for a private repo and is not enough for a
public one. A capture corpus taken from a real account also carries:

  identity      nick_name, nickname, third_id, avatar URL
  biometrics    birthday, gender, height, weight
  hardware      MAC address, serial number, device id, bind_device, uuid

None of those are credentials, so none were touched. All of them identify a
person, and the MAC address and serial identify a specific physical device
that can be correlated across datasets.

**This file contains no real identifiers.** An earlier version hard-coded
them, which meant publishing the anonymiser published the very handle, serial
and MAC it was written to remove. The values now live in the git-ignored
`identities.local.json`; see `identities.example.json` for the format.

It is deterministic and idempotent: running twice produces the same bytes.

What it deliberately does NOT remove:

  * the physiological series (steps, sleep stages, heart rate). These are the
    substance of every decoder test -- a corpus without them proves nothing.
    Detached from name, birthday, device and location they are just numbers.
  * the timezone. Two tests assert on it, it is shared with a billion-odd
    people, and removing it would delete the only evidence the local-time
    handling works.

Run from the repo root:  uv run tools/anonymize_fixtures.py
"""

from __future__ import annotations

import base64
import binascii
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "zepp"
IDENTITIES = Path(__file__).parent / "identities.local.json"
EXAMPLE = Path(__file__).parent / "identities.example.json"

# Keys whose value is replaced outright, whatever it holds. These are
# generic Zepp field names and the replacements are obvious fakes, so both
# sides are safe to publish.
KEY_REPLACEMENTS: dict[str, object] = {
    "avatar": "<avatar_url>",
    "birthday": "1990-01",
    "nick_name": "Alex",
    "nickname": "example_user",
    "third_id": "THIRD_PARTY_ID_REDACTED_0000000000",
}

# Biometrics. Replaced with round, plainly-synthetic values rather than
# deleted, so a decoder expecting the field still finds a number.
BIOMETRIC_REPLACEMENTS: dict[str, object] = {
    "height": 175,
    "weight": 70.0,
    "weight_float": 70.0,
    "gender": 0,
}

# Base64-wrapped JSON that must be decoded, rewritten, re-encoded: `summary`
# in a band_data row hides the watch serial inside `sn`. `data` and `data_hr`
# are binary sensor blobs and are left alone.
B64_JSON_KEYS = ("summary",)


def load_identities() -> tuple[list[tuple[str, str]], dict[int, int]]:
    """Read the local identifier map.

    Returns string substitutions ordered LONGEST FIRST -- a short device id
    is a substring of the composites that embed it ("run.<id>.huami.com",
    "0:MILI:<id>:..."), and replacing it first would leave those
    half-substituted.
    """
    if not IDENTITIES.is_file():
        sys.exit(
            f"Missing {IDENTITIES.name}.\n\n"
            f"Copy {EXAMPLE.name} to {IDENTITIES.name} and fill in the\n"
            "identifiers from your own captures. It is git-ignored on purpose:\n"
            "it is a plaintext list of your personal identifiers."
        )
    config = json.loads(IDENTITIES.read_text())
    strings = {
        str(k): str(v) for k, v in (config.get("strings") or {}).items()
        if not k.startswith("_")
    }
    integers = {
        int(k): int(v) for k, v in (config.get("integers") or {}).items()
        if not str(k).startswith("_")
    }
    ordered = sorted(strings.items(), key=lambda kv: len(kv[0]), reverse=True)
    return ordered, integers


class Anonymiser:
    def __init__(self, strings: list[tuple[str, str]], integers: dict[int, int]):
        self._strings = strings
        self._integers = integers

    def text(self, value: str) -> str:
        for old, new in self._strings:
            value = value.replace(old, new)
        return value

    def _b64_json(self, encoded: str) -> str:
        try:
            payload = json.loads(base64.b64decode(encoded, validate=True))
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return encoded  # binary sensor blob, not JSON
        return base64.b64encode(
            json.dumps(self.walk(payload), separators=(",", ":")).encode()
        ).decode()

    def walk(self, obj: object) -> object:
        if isinstance(obj, dict):
            out: dict[object, object] = {}
            for key, value in obj.items():
                name = str(key)
                if name in KEY_REPLACEMENTS:
                    out[key] = KEY_REPLACEMENTS[name]
                elif name in BIOMETRIC_REPLACEMENTS and isinstance(
                    value, (int, float, str)
                ):
                    out[key] = BIOMETRIC_REPLACEMENTS[name]
                elif name in B64_JSON_KEYS and isinstance(value, str) and value.strip():
                    out[key] = self._b64_json(value)
                else:
                    out[key] = self.walk(value)
            return out
        if isinstance(obj, list):
            return [self.walk(item) for item in obj]
        if isinstance(obj, str):
            return self.text(obj)
        # bool subclasses int; leave flags alone.
        if isinstance(obj, int) and not isinstance(obj, bool):
            return self._integers.get(obj, obj)
        return obj


def main() -> int:
    if not FIXTURES.is_dir():
        sys.exit(f"No fixture corpus at {FIXTURES}")

    strings, integers = load_identities()
    anonymiser = Anonymiser(strings, integers)

    rewritten = 0
    dropped_body_text = 0
    for path in sorted(FIXTURES.glob("*.json")):
        original = path.read_text()
        record = json.loads(original)

        if isinstance(record, dict) and record.pop("body_text", None) is not None:
            # body_text duplicates body_parsed, roughly doubling the corpus
            # size, and is a second surface every scrub has to reach. The
            # decoders read body_parsed.
            dropped_body_text += 1

        updated = json.dumps(anonymiser.walk(record), indent=2, ensure_ascii=False)
        if updated != original:
            path.write_text(updated)
            rewritten += 1

    total = len(list(FIXTURES.glob("*.json")))
    print(f"rewrote {rewritten} of {total} fixtures")
    if dropped_body_text:
        print(f"dropped body_text from {dropped_body_text}")
    print("\nNow verify:  uv run tools/check_fixtures.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
