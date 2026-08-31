import type {
  AccessMode,
  ConfigDescriptor,
  JsonObject,
  RunDescriptor,
  RunMode,
} from "@aihi/code-protocol";
import { isAccessMode, isJsonObject, isRunMode } from "@aihi/code-protocol";

export interface EffectiveAuthority {
  accessMode: AccessMode;
  runMode: RunMode;
}

export function configuredAuthority(
  config: Pick<ConfigDescriptor, "access_mode" | "run_mode">,
): EffectiveAuthority {
  return { accessMode: config.access_mode, runMode: config.run_mode };
}

export function effectiveAuthority(
  config: Pick<ConfigDescriptor, "access_mode" | "run_mode">,
  runs: readonly Pick<RunDescriptor, "run_id" | "access_mode" | "run_mode">[],
  activeRunId?: string,
): EffectiveAuthority {
  const fallback = configuredAuthority(config);
  const selected = activeRunId === undefined
    ? runs[0]
    : runs.find((run) => run.run_id === activeRunId) ?? runs[0];
  if (selected === undefined) return fallback;
  return {
    accessMode: selected.access_mode ?? fallback.accessMode,
    runMode: selected.run_mode ?? fallback.runMode,
  };
}

export function authorityFromEventData(
  data: JsonObject,
  current: EffectiveAuthority,
): EffectiveAuthority {
  const profile = data.application_profile;
  if (!isJsonObject(profile)) return current;
  if (!isAccessMode(profile.access_mode) || !isRunMode(profile.run_mode)) return current;
  return { accessMode: profile.access_mode, runMode: profile.run_mode };
}

export function formatAuthority(authority: EffectiveAuthority): string {
  return `${authority.accessMode} · ${authority.runMode}`;
}
