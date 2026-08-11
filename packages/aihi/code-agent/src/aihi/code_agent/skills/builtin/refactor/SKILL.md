---
name: refactor
description: Restructure code without changing its observable behaviour.
version: 1.0.0
allowed_tools: [read_file, glob, grep, edit_file, write_file, bash, git_diff]
---

Refactoring changes structure, never observable behaviour. If behaviour must
change, that is a separate change with its own tests.

## Order of work

1. Confirm there are tests covering the behaviour you are about to move. If
   there are none, write them first and see them pass against the current code.
2. Make one structural change at a time and run the tests after each.
3. Keep the public surface stable, or update every caller in the same change.
   Leave no half-migrated state.

## What justifies a refactor

A file doing several unrelated jobs, a boundary that leaks internals, logic
duplicated in a way that has already drifted, or a name that misdescribes what
the code does.

## What does not

Personal preference, speculative generality for a requirement nobody has, or
churn bundled into an unrelated change. Do not refactor code the current task
does not touch.
