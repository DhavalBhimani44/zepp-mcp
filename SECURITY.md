# Security Policy

## Reporting a vulnerability

**Do not open a public issue for security problems**, especially anything
involving credentials.

Report privately via
[GitHub Security Advisories](https://github.com/DhavalBhimani44/zepp-mcp/security/advisories/new).

Please include what the issue is, how to reproduce it, and what an attacker
could achieve. You can expect an initial response within a week.

## Scope

This project handles Zepp account credentials and personal health data. The
following are in scope:

- Credential exposure — in logs, error messages, fixtures, config files or the
  token cache
- Personal data leaking into the committed fixture corpus
- Anything that causes health data to be written to disk unexpectedly
- Auth flow weaknesses, including anything that could trigger the account
  lockout

Out of scope: vulnerabilities in the Zepp API itself. Report those to Zepp
Health.

## What this project already does

- Credentials are read from `.env` or the environment. They are never embedded
  in source, written to captures, or printed. `.env` is git-ignored.
- Only the API token is cached, at `~/.zepp-mcp/token.json` with mode `0600`.
  No health data is written to disk.
- HTTP request logging is off by default, because the request line contains
  the user id. `ZEPP_DEBUG=1` re-enables it.
- Login makes exactly one attempt and never retries. Zepp enforces a shared
  10-attempt lockout across hosts and regions.
- The fixture corpus is anonymised, and `tools/check_fixtures.py` runs in CI
  on every push to keep it that way.

## If you think a credential leaked

1. Change your Zepp password immediately.
2. Delete the token cache: `rm ~/.zepp-mcp/token.json`
3. Then report it.
