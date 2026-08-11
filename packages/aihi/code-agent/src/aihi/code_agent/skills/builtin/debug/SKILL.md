---
name: debug
description: Find the root cause of a failure before proposing any fix.
version: 1.0.0
allowed_tools: [read_file, glob, grep, bash, git_diff]
---

Find the root cause before you change anything. A fix that removes the symptom
without explaining it is not a fix.

## Order of work

1. Read the error text and stack trace completely. Note the exact file, line
   and message rather than paraphrasing them.
2. Reproduce it. If you cannot reproduce it reliably, gather more evidence
   instead of guessing.
3. Check what changed recently — `git_diff` and history around the failing
   code.
4. In a multi-component path, instrument the boundaries and run once to see
   which component actually breaks, then investigate that one.
5. Trace the bad value backwards to where it originates. Fix it there, not
   where it surfaced.

## Before claiming a fix

State the hypothesis as one sentence: "X is the cause because Y." Change one
thing to test it. If the change does not work, form a new hypothesis rather
than stacking another fix on top. After three failed fixes, stop and question
the design instead of trying a fourth.

Write a failing test that reproduces the bug before fixing it, and report the
real command output that shows it now passes.
