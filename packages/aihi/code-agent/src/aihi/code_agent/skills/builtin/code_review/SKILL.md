---
name: code_review
description: Review changed code for correctness defects that would actually bite, not for style.
version: 1.0.0
allowed_tools: [read_file, glob, grep, git_status, git_diff]
---

Review changed code for defects that would actually bite, not for style.

## Order of work

1. Read the full diff first (`git_diff`). Do not review a file you have not
   read in its surrounding context.
2. For each change, ask what input or state would make it wrong. A finding you
   cannot turn into a concrete failure scenario is not a finding.
3. Check the callers of anything whose signature, return type, or error
   behaviour changed.

## What counts as a finding

- Correctness: wrong results, unhandled error paths, broken invariants,
  off-by-one, resource leaks, concurrency hazards.
- Security: injection, path traversal, secrets in code or logs, missing
  authorization on a state change.
- Regression risk: silent behaviour changes to an existing public interface.

## What does not

- Formatting a linter already enforces.
- Restating what the code does.
- Preferences with no defect behind them.

## Reporting

State each finding as: file:line, one sentence naming the defect, then the
concrete input or state that triggers it. Rank by severity. If nothing survives
that bar, say the diff looks correct and stop — do not pad the list.
