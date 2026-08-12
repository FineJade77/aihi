import type { ApprovalResolution, RunAccepted } from "@aihi/code-protocol";
import type { ApprovalResolveParams, RpcClient } from "./rpc/client.js";

type ApprovalClient = Pick<RpcClient, "resolveApproval" | "resumeRun">;

/** Resolve and resume as one UI action; denial must also reach the model as a ToolResult. */
export async function resolveApprovalAndResume(
  client: ApprovalClient,
  params: ApprovalResolveParams,
): Promise<{ resolution: ApprovalResolution; run: RunAccepted }> {
  const resolution = await client.resolveApproval(params);
  const run = await client.resumeRun({
    session_id: params.session_id,
    run_id: resolution.run_id,
  });
  return { resolution, run };
}
