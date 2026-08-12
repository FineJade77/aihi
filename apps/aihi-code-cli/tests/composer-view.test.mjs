import assert from "node:assert/strict";
import { PassThrough } from "node:stream";
import test from "node:test";
import { render } from "ink";
import React, { useState } from "react";
import { ComposerInput } from "../dist/tui/composer-view.js";
import { createComposerState } from "../dist/tui/composer.js";
import { SLASH_COMMANDS } from "../dist/tui/commands.js";

const tick = () => new Promise((resolve) => setTimeout(resolve, 25));

function harness() {
  const stdin = new PassThrough();
  stdin.isTTY = true;
  stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin;
  stdin.unref = () => stdin;
  const stdout = new PassThrough();
  stdout.columns = 100;
  stdout.rows = 30;
  const stderr = new PassThrough();
  const submitted = [];
  let currentState = createComposerState(["previous prompt"]);
  const stateHistory = [];
  let started = false;

  function TestApp() {
    const [state, setState] = useState(() => currentState);
    return React.createElement(ComposerInput, {
      state,
      commands: SLASH_COMMANDS,
      onChange: (next) => {
        currentState = next;
        stateHistory.push(next.value);
        setState(next);
      },
      onSubmit: (value) => submitted.push(value),
    });
  }

  const application = render(React.createElement(TestApp), {
    stdin,
    stdout,
    stderr,
    debug: true,
    exitOnCtrlC: false,
    patchConsole: false,
  });
  const start = async () => {
    if (started) return;
    for (let attempt = 0; attempt < 40; attempt += 1) {
      if (stdin.listenerCount("readable") > 0) {
        started = true;
        return;
      }
      await tick();
    }
    assert.fail("Ink input listener did not start");
  };
  return {
    application,
    stdin,
    stdout,
    submitted,
    stateHistory,
    start,
    getState: () => currentState,
  };
}

async function waitFor(predicate, message) {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (predicate()) return;
    await tick();
  }
  assert.fail(message);
}

test("Ink composer completes slash commands and submits through terminal input", async () => {
  const { application, stdin, stdout, submitted, stateHistory, getState, start } = harness();
  try {
    await start();
    stdin.write("/skill-");
    await waitFor(() => getState().value === "/skill-", "Slash prefix was not entered");
    assert.match(stdout.read().toString(), /skill-trust/);

    stdin.write("\t");
    await waitFor(
      () => getState().value !== "/skill-",
      `Tab did not complete slash command; state=${JSON.stringify(getState())}`,
    );
    assert.equal(
      getState().value,
      "/skill-trust ",
      `state history=${JSON.stringify(stateHistory)} submitted=${JSON.stringify(submitted)}`,
    );
    stdin.write("demo");
    await tick();
    stdin.write("\r");
    await tick();

    assert.deepEqual(submitted, ["/skill-trust demo"]);
  } finally {
    application.unmount();
  }
});

test("Ink composer preserves Ctrl-J as a newline and recalls command history", async () => {
  const { application, stdin, submitted, getState, start } = harness();
  try {
    await start();
    stdin.write("first");
    await tick();
    stdin.write("\x0a");
    await tick();
    stdin.write("second");
    await tick();
    stdin.write("\r");
    await tick();
    assert.deepEqual(submitted, ["first\nsecond"]);

    stdin.write("\u001b[A");
    await waitFor(() => getState().value === "first\nsecond", "Up did not recall history");
    stdin.write("\r");
    await tick();
    assert.deepEqual(submitted, ["first\nsecond", "first\nsecond"]);
  } finally {
    application.unmount();
  }
});

test("Ink composer ignores global Ctrl shortcuts without changing its draft", async () => {
  const { application, stdin, getState, start } = harness();
  try {
    await start();
    stdin.write("draft");
    await waitFor(() => getState().value === "draft", "Draft was not entered");
    stdin.write("\x0f"); // Ctrl-O belongs to the Tool detail viewport shortcut.
    await tick();
    assert.equal(getState().value, "draft");
  } finally {
    application.unmount();
  }
});

test("Ink composer clears its draft with Escape", async () => {
  const { application, stdin, getState, start } = harness();
  try {
    await start();
    stdin.write("draft");
    await waitFor(() => getState().value === "draft", "Draft was not entered");
    stdin.write("\u001b");
    await waitFor(() => getState().value === "", "Escape did not clear the draft");
  } finally {
    application.unmount();
  }
});
