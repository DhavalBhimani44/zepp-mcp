"""Strip credentials from captured responses before they touch disk."""

from __future__ import annotations

from typing import Any

# NOTE: "code" is deliberately absent. Zepp's standard response envelope is
# {"code": 1, "data": [...]} where code is an HTTP-ish status, not a secret —
# redacting it would destroy the status field in every single capture. The
# OAuth access code never reaches a capture under a bare "code" key; it is
# registered as a substring in auth.py before its response is recorded.
SENSITIVE_KEYS = frozenset({
    "app_token", "apptoken", "login_token", "access", "refresh",
    "token", "password", "emailorphone", "email",
    "user_id", "userid", "uid", "device_id",
    # Location, not credentials. Workout summary rows carry `location` as a
    # 12-character geohash and `city` as plain text; a route starts where the
    # athlete lives, so these are PII in a corpus bound for a public repo.
    # GPS *streams* (longitude_latitude, altitude, accuracy) are not scrubbed
    # here — they are structural, so those captures are excluded wholesale at
    # promotion time instead. See promote.py.
    "location", "city",
})

MIN_SUBSTRING_LEN = 8


class Redactor:
    def __init__(self) -> None:
        self._substrings: list[tuple[str, str]] = []

    def register(self, value: str | None, label: str) -> None:
        """Register a secret for substring replacement everywhere it appears.

        Values shorter than MIN_SUBSTRING_LEN are ignored to avoid corrupting
        unrelated data that happens to contain the same characters. Those are
        still caught by key name if they appear under a SENSITIVE_KEYS field.
        """
        if not value or len(str(value)) < MIN_SUBSTRING_LEN:
            return
        self._substrings.append((str(value), f"<{label}>"))
        # Longest first, so an overlapping longer secret wins.
        self._substrings.sort(key=lambda pair: len(pair[0]), reverse=True)

    def scrub(self, obj: Any) -> Any:
        if isinstance(obj, str):
            for actual, placeholder in self._substrings:
                obj = obj.replace(actual, placeholder)
            return obj
        if isinstance(obj, dict):
            out: dict[Any, Any] = {}
            for key, value in obj.items():
                if str(key).lower() in SENSITIVE_KEYS and isinstance(value, (str, int)):
                    out[key] = f"<{key}>"
                else:
                    out[key] = self.scrub(value)
            return out
        if isinstance(obj, list):
            return [self.scrub(item) for item in obj]
        return obj
