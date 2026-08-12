import { constants } from "node:fs";
import { access, stat } from "node:fs/promises";
import { dirname } from "node:path";
import type { AuditDescriptor } from "@aihi/code-protocol";

function errorCode(error: unknown): string | undefined {
  if (error !== null && typeof error === "object" && "code" in error) {
    const code = (error as { code?: unknown }).code;
    return typeof code === "string" ? code : undefined;
  }
  return undefined;
}

/** Check the local audit destination without creating or modifying it. */
export async function auditDiagnostic(audit: AuditDescriptor | undefined): Promise<string> {
  if (audit === undefined) return "! audit · descriptor unavailable";
  if (!audit.enabled) return "✓ audit · disabled";
  if (typeof audit.path !== "string" || audit.path.length === 0) {
    return "! audit · enabled but no path is configured";
  }

  try {
    const details = await stat(audit.path);
    if (!details.isFile()) return `! audit · path is not a regular file · ${audit.path}`;
    await access(audit.path, constants.W_OK);
    return `✓ audit · writable · ${audit.path}`;
  } catch (error) {
    if (errorCode(error) !== "ENOENT") {
      return `! audit · not writable · ${audit.path}`;
    }
  }

  // JsonlTelemetrySink creates missing parents at runtime. Find the nearest
  // existing directory so doctor can validate that creation should succeed.
  let parent = dirname(audit.path);
  while (true) {
    try {
      const details = await stat(parent);
      if (!details.isDirectory()) return `! audit · parent is not a directory · ${parent}`;
      await access(parent, constants.W_OK);
      return `✓ audit · ready (will create file) · ${audit.path}`;
    } catch (error) {
      if (errorCode(error) !== "ENOENT") {
        return `! audit · parent is not writable · ${parent}`;
      }
      const next = dirname(parent);
      if (next === parent) return `! audit · no existing parent directory · ${audit.path}`;
      parent = next;
    }
  }
}
