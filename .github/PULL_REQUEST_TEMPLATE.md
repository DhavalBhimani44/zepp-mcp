## What this changes

<!-- What does this do, and why? If it fixes a decoder, what was the evidence? -->

Fixes #

## Type

- [ ] Bug fix
- [ ] New tool or capability
- [ ] Sport code / unit confirmation
- [ ] Documentation
- [ ] Refactor or tooling

## Verification

- [ ] `uv run pytest` passes
- [ ] `uv run tools/smoke_test.py` passes
- [ ] `uv run tools/check_fixtures.py` passes

## If this touches decoding

- [ ] The encoding (delta vs absolute) is proven against a total the payload
      reports independently
- [ ] The unit is proven, or marked `unit_verified: false` and left unconverted
- [ ] The field's sentinel value is handled
- [ ] Unrecognised fields are passed through raw rather than guessed at

## If this adds or changes fixtures

- [ ] `uv run tools/anonymize_fixtures.py` was run
- [ ] `uv run tools/check_fixtures.py` passes
- [ ] No GPS-bearing capture is included
- [ ] Any new identifier is added to the anonymiser **and** the privacy gate
