import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { auditDiagnostic } from "../dist/tui/doctor.js";

test("audit doctor reports disabled and missing destinations without creating files", async () => {
  const root = await mkdtemp(join(tmpdir(), "aihi-doctor-"));
  const path = join(root, "nested", "audit.jsonl");

  assert.equal(await auditDiagnostic({ enabled: false, path }), "✓ audit · disabled");
  assert.equal(
    await auditDiagnostic({ enabled: true, path }),
    `✓ audit · ready (will create file) · ${path}`,
  );
});

test("audit doctor checks regular-file writability", async () => {
  const root = await mkdtemp(join(tmpdir(), "aihi-doctor-"));
  const path = join(root, "audit.jsonl");
  await writeFile(path, "{}\n", "utf8");
  assert.equal(await auditDiagnostic({ enabled: true, path }), `✓ audit · writable · ${path}`);

  const directory = join(root, "directory");
  await mkdir(directory);
  assert.equal(
    await auditDiagnostic({ enabled: true, path: directory }),
    `! audit · path is not a regular file · ${directory}`,
  );
});
