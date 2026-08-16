# Contributing to zepp-mcp

Thanks for considering a contribution. This project maps an undocumented API,
so the most valuable contributions are often not code — a sport code confirmed
against your own watch is worth more than a refactor.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Contents

- [The one rule that matters](#the-one-rule-that-matters)
- [Getting set up](#getting-set-up)
- [What to work on](#what-to-work-on)
- [Adding fixtures](#adding-fixtures)
- [Testing philosophy](#testing-philosophy)
- [Decoding new fields](#decoding-new-fields)
- [Style](#style)
- [Pull requests](#pull-requests)

## The one rule that matters

**Never commit personal data.**

The fixture corpus comes from a real Zepp account. Every capture carries
identity, biometrics, hardware serials and sometimes location. A published
git history is not undoable.

Before every commit:

```bash
uv run tools/check_fixtures.py
```

CI runs this on every push and pull request. It fails the build on names, MAC
addresses, serial numbers, emails, coordinate streams and credential-shaped
tokens — including values hidden inside base64 payloads.

If it fails, do not work around it. Run the anonymiser and re-check.

## Getting set up

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
git clone https://github.com/DhavalBhimani44/zepp-mcp.git
cd zepp-mcp
uv sync

uv run pytest               # 28 tests, no network, no credentials
uv run tools/smoke_test.py  # server starts and lists its tools
uv run tools/check_fixtures.py
```

Install the pre-push hook. It refuses to push a branch holding
pre-anonymisation history, runs the privacy gate, and checks that no
credential file is tracked:

```bash
ln -sf ../../tools/hooks/pre-push .git/hooks/pre-push
```

> [!IMPORTANT]
> Force pushes are blocked on the default branch, so a bad push **cannot be
> rewritten away**. The usual remediation — rewrite history, force push over
> it — is unavailable. Anything that lands is permanent short of deleting the
> repository. That is why the check runs before the push rather than after.

Everything above works without a Zepp account. You only need credentials to
test against live data:

```bash
cp .env.example .env   # add ZEPP_EMAIL and ZEPP_PASSWORD
chmod 600 .env
```

> [!WARNING]
> Zepp enforces a **shared 10-attempt lockout** across hosts and regions. The
> login code deliberately makes exactly one attempt and never retries. Do not
> add a retry loop, and do not write tests that log in repeatedly.

Remember that MCP clients spawn a fresh server process per session, so
**restart your client after changing code**.

## What to work on

Good first contributions, roughly in order of value:

1. **Confirm a sport code.** The payload contains no sport name — only a
   number. `zepp_mcp/codes.py` maps the five codes seen on one account. If
   your watch records a sport that reports as `unknown_sport_<code>`, open an
   issue with the code and what the Zepp app calls it.
2. **Confirm a flagged unit.** Anything with `unit_verified: false`, plus the
   `te` / `anaerobic_te` scaling question. Compare a value against what the
   Zepp app displays for the same workout.
3. **Add a tool for an unmodelled metric.** PAI, SpO₂, stress, HRV,
   respiratory rate, readiness, Body Charge and weight all return data but
   are reachable only through `zepp_raw_request`.
4. **Test multisport.** No triathlon has ever been recorded against this
   code. If you have one, an anonymised fixture would be genuinely valuable.
5. **The GPS decoder.** Untested by design, since the corpus excludes
   GPS-bearing workouts. A synthetic track would unblock it.

Open an issue before large changes so we can agree on the approach.

## Adding fixtures

Fixtures are real API captures. They are what make the decoder tests
meaningful, and they are the highest-risk thing in the repo.

1. Capture with the spike probe, which writes to the git-ignored `spike/out/`:

   ```bash
   cd spike && uv run probe.py
   ```

2. Promote captures into the corpus:

   ```bash
   cd spike && uv run promote.py
   ```

   This excludes GPS-bearing workouts wholesale and re-scrubs everything else.

3. **Anonymise**, which strips identity, biometrics and hardware ids —
   including values inside base64-wrapped JSON.

   First tell it what *your* identifiers are. This file is git-ignored
   because it is a plaintext list of them:

   ```bash
   cp tools/identities.example.json tools/identities.local.json
   chmod 600 tools/identities.local.json
   # fill in your handle, name, device id, serial, MAC, uuid
   ```

   Then run it:

   ```bash
   uv run tools/anonymize_fixtures.py
   ```

   The anonymiser and the privacy gate deliberately contain **no real
   identifiers**. An early version hard-coded them, which meant publishing
   the anonymiser published the very serial and MAC it existed to remove.

4. **Verify**, and read the output rather than trusting the exit code:

   ```bash
   uv run tools/check_fixtures.py
   ```

If your capture contains an identifier the anonymiser does not know about,
add it to `identities.local.json` under `strings` or `integers` — Zepp sends
the same id as both a string and a bare integer, and string replacement
cannot see the integer form. If it is a whole *field* rather than a value,
add it to `KEY_REPLACEMENTS` in the anonymiser and to `EXPECTED_PLACEHOLDERS`
in `tools/check_fixtures.py` so the gate keeps enforcing it.

The gate checks three layers: structural shapes (emails, MACs, coordinate
streams), placeholder assertions (anonymised fields hold the expected fake
value), and — if you have `identities.local.json` — that none of your own
originals survived. CI can only run the first two, which is why the
placeholder layer matters.

Keep fixtures as small as the test needs. The corpus is already ~8 MB.

## Testing philosophy

**Assertions should be anchored to values the API itself reported.**

The strongest tests here check a decoder against Zepp's own arithmetic in the
same payload, so drift is caught by the data rather than by a number someone
typed into a test:

```python
def test_distance_stream_matches_the_summary_total(swim_detail, history_rows):
    """currentDistance is absolute centimetres, not a delta."""
    stream = decode.decode_stream("currentDistance", swim_detail["currentDistance"])
    swim = next(r for r in history_rows if str(r["trackid"]) == "1786761306")
    assert stream["max"] == pytest.approx(float(swim["dis"]), rel=0.01)
```

That test would fail if the encoding assumption were wrong, which a
hand-written expected value would not.

Tests must not require network or credentials. All 28 run against the corpus.

## Decoding new fields

The project has one consistent rule: **a field is decoded and unit-attributed
only when the corpus proves its meaning.**

When adding a decoder:

- **Prove the encoding.** Delta or absolute is per field. Decode both ways and
  check against a total the payload reports independently.
- **Prove the unit.** If you cannot, mark it `unit_verified: false` and leave
  the value unconverted. Do not guess a unit that makes the number look right.
- **Handle the sentinel.** Find which not-applicable marker the field uses —
  they vary (`-1`, `-20000`, `-361`, `-100`, `-274`, `0`) — and add it to the
  right family in `zepp_mcp/codes.py`.
- **Watch the type.** Zepp sends the same field as a string in one endpoint
  and an integer in another. Coerce before comparing.
- **Pass through what you don't know.** Unrecognised fields belong under
  `raw`, untouched.

The failure mode this guards against is specific: the consumer is an LLM that
will restate whatever it is given as fact. A wrongly decoded stream does not
look wrong — it looks like data.

## Style

- Follow the surrounding code. Type hints on public functions.
- Comments explain **why**, not what. If a line encodes a hard-won fact about
  the API, say what the evidence was.
- No new runtime dependencies without discussion.
- Keep modules focused; prefer a new module over growing an existing one past
  its responsibility.

## Pull requests

1. Fork and branch from `main`.
2. Make the change, with tests.
3. Run the full gate:

   ```bash
   uv run pytest
   uv run tools/smoke_test.py
   uv run tools/check_fixtures.py
   ```

4. Write a commit message that explains the reasoning, not just the change.
   If you fixed a decoder, say what the evidence was.
5. Open the PR and fill in the template.

Small, focused PRs get reviewed faster. If you're unsure whether something
fits, open an issue first — that's always welcome.
