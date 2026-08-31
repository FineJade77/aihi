import type { ProviderDescriptor, SessionDescriptor } from "@aihi/code-protocol";

export type PickerMode = "session" | "provider" | "model";

export interface PickerOption {
  /** Stable key used by Ink when the filtered window changes. */
  key: string;
  /** Value passed to the existing slash command/RPC when selected. */
  value: string;
  label: string;
  detail: string;
  searchText: string;
  sessionId?: string;
  provider?: string;
  model?: string;
}

export interface PickerState {
  mode: PickerMode;
  query: string;
  selectedIndex: number;
  options: PickerOption[];
}

function metadataText(session: SessionDescriptor, key: string): string | undefined {
  const value = session.metadata[key];
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

export function sessionPickerOptions(sessions: readonly SessionDescriptor[]): PickerOption[] {
  return sessions.map((session) => {
    const sessionCwd = session.cwd;
    const sessionProvider = metadataText(session, "provider");
    const sessionModel = metadataText(session, "model");
    const title = metadataText(session, "title") ?? `Session ${session.session_id.slice(-12)}`;
    const detail = [
      sessionProvider && sessionModel ? `${sessionProvider}/${sessionModel}` : sessionProvider ?? sessionModel,
      sessionCwd,
      `seq ${session.head_seq}`,
    ].filter(Boolean).join(" · ");
    return {
      key: session.session_id,
      value: session.session_id,
      label: title,
      detail,
      searchText: `${title} ${session.session_id} ${detail}`,
      sessionId: session.session_id,
    };
  });
}

export function providerPickerOptions(providers: readonly ProviderDescriptor[]): PickerOption[] {
  return providers.map((provider) => ({
    key: provider.name,
    value: provider.name,
    label: provider.name,
    detail: `${provider.models?.length ?? 1} model${(provider.models?.length ?? 1) === 1 ? "" : "s"} · ${provider.model}${provider.base_url ? ` · ${provider.base_url}` : ""}`,
    searchText: `${provider.name} ${(provider.models ?? [provider.model]).join(" ")} ${provider.base_url ?? ""}`,
    provider: provider.name,
    model: provider.model,
  }));
}

export function modelPickerOptions(providers: readonly ProviderDescriptor[]): PickerOption[] {
  return providers.flatMap((provider) =>
    (provider.models ?? [provider.model]).map((model) => ({
      key: `${provider.name}/${model}`,
      value: model,
      label: model,
      detail: provider.name + (provider.base_url ? ` · ${provider.base_url}` : ""),
      searchText: `${model} ${provider.name} ${provider.base_url ?? ""}`,
      provider: provider.name,
      model,
    })),
  );
}

export function filterPickerOptions(
  options: readonly PickerOption[],
  query: string,
): PickerOption[] {
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized) return [...options];
  const terms = normalized.split(/\s+/).filter(Boolean);
  return options.filter((option) => {
    const haystack = option.searchText.toLocaleLowerCase();
    return terms.every((term) => haystack.includes(term));
  });
}

export function movePickerSelection(
  selectedIndex: number,
  count: number,
  direction: "up" | "down",
): number {
  if (count <= 0) return 0;
  const next = direction === "down" ? selectedIndex + 1 : selectedIndex - 1;
  return (next + count) % count;
}

export function pickerTitle(mode: PickerMode): string {
  if (mode === "session") return "SELECT SESSION";
  if (mode === "provider") return "SELECT PROVIDER";
  return "SELECT MODEL";
}
