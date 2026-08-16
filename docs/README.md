# Documentation

## Start here

**[api-findings.md](api-findings.md)** — what the Zepp API actually does.
Endpoint inventory, payload encodings, sentinel values, and the twelve
questions the reconnaissance spike answered. This is the reference for
anyone writing a decoder.

**[design-spec.md](design-spec.md)** — why the server is shaped the way it
is. Tool surface, failure semantics, the derived-vs-measured rule, and the
storage decision.

## History

Kept for provenance. Neither describes the current implementation.

**[history/architecture-draft-v1.md](history/architecture-draft-v1.md)** —
the original design, written before any API call was made. Its central
premise turned out to be **wrong**: it assumed the cloud API discards data
after a day, which is true of the on-watch Zepp OS sensor API but not of the
REST API this project uses. That error drove a local-database design the
spec later removed. Preserved because being able to see a wrong assumption
and its correction is more useful than a clean history.

**[history/recon-spike-plan.md](history/recon-spike-plan.md)** — the
task-by-task plan for the throwaway probe in `spike/` that produced the
fixture corpus.
