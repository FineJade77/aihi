---
name: test_writing
description: Write tests that would fail if the behaviour they describe regressed.
version: 1.0.0
allowed_tools: [read_file, glob, grep, edit_file, write_file, bash]
---

Write tests that fail when the behaviour breaks, and pass for the right reason.

## Order of work

1. Read the existing tests first and match their structure, naming, fixtures
   and assertion style.
2. Name each test after the behaviour it protects, not after the function it
   calls.
3. Write the test before the implementation when adding behaviour. Run it and
   watch it fail with the message you expect — a test that has never failed has
   not been shown to test anything.
4. Assert on observable outcomes, not on internal calls, unless the interaction
   itself is the contract.

## What to cover

The behaviour the change actually introduces, its error path, and the boundary
where it stops applying. Prefer one clear test per behaviour over a single test
with many assertions.

## What to avoid

- Tests that restate the implementation line by line.
- Mocks that would keep passing if the real dependency changed its contract.
- Sleeping on a timer instead of waiting for a condition.
