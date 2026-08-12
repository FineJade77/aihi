import { createContext, useContext } from "react";

/**
 * The TUI palette.
 *
 * Terminals do not tell us their background reliably, and the 16-colour names
 * ("gray", "blue", …) are whatever the user's colour scheme says they are —
 * on most dark schemes "gray" is bright-black, which lands near #4d4d4d and
 * disappears against the window. Every tone here is therefore an explicit hex
 * value chosen for a contrast ratio against its own background family:
 * >= 4.5:1 for text, >= 3:1 for chrome. Chalk downgrades hex on terminals
 * without truecolor, so the values stay safe on 256- and 16-colour hosts.
 */
export interface Theme {
  name: ThemeName;
  /** Undefined means "the terminal's own foreground", always maximal contrast. */
  text: string | undefined;
  /** Secondary text: labels, hints, ids. Must stay legible, never decorative. */
  muted: string;
  /** Tertiary text: keymap footers and other text you read once. */
  faint: string;
  brand: string;
  accent: string;
  good: string;
  warn: string;
  bad: string;
  border: string;
  gradientStart: string;
  gradientEnd: string;
}

export type ThemeName = "dark" | "light";

/** Contrast on #0d1117 / #1e1e1e / #282c34: muted 5.6–7.6, faint 3.3–4.4. */
const DARK: Theme = {
  name: "dark",
  text: undefined,
  muted: "#9aa5b4",
  faint: "#6f7b8d",
  brand: "#45c8d6",
  accent: "#a78bfa",
  good: "#57d38c",
  warn: "#e8b339",
  bad: "#ff7b72",
  border: "#6f7b8d",
  gradientStart: "#7c5cff",
  gradientEnd: "#c9a6ff",
};

/** Contrast on #ffffff / #fdf6e3: muted 5.4–5.8, faint 3.6–3.9. */
const LIGHT: Theme = {
  name: "light",
  text: undefined,
  muted: "#5c6672",
  faint: "#78828f",
  brand: "#0e7490",
  accent: "#7c3aed",
  good: "#15803d",
  warn: "#8f5f00",
  bad: "#b42318",
  border: "#7c8794",
  gradientStart: "#6d28d9",
  gradientEnd: "#a855f7",
};

export function createTheme(name: ThemeName): Theme {
  return name === "light" ? LIGHT : DARK;
}

/**
 * Picks the palette from the environment.
 *
 * AIHI_THEME is the explicit escape hatch. Otherwise COLORFGBG — set by
 * iTerm2, Konsole and rxvt as "<fg>;<bg>" — names the background's ANSI index,
 * of which 7 (light grey) and 9-15 (the bright half) are light schemes. When
 * nothing says otherwise we assume dark, which is what terminals ship with.
 */
export function resolveThemeName(env: NodeJS.ProcessEnv = process.env): ThemeName {
  const explicit = env.AIHI_THEME?.trim().toLowerCase();
  if (explicit === "dark" || explicit === "light") return explicit;
  const fields = env.COLORFGBG?.split(";");
  const background = fields?.[fields.length - 1]?.trim();
  if (background !== undefined && /^\d+$/.test(background)) {
    const index = Number(background);
    return index === 7 || index >= 9 ? "light" : "dark";
  }
  return "dark";
}

const ThemeContext = createContext<Theme>(createTheme(resolveThemeName()));

export const ThemeProvider = ThemeContext.Provider;

export function useTheme(): Theme {
  return useContext(ThemeContext);
}

/** Interpolates the wordmark ramp between the palette's two brand purples. */
export function gradientColor(theme: Theme, ratio: number): string {
  const clamped = Math.min(1, Math.max(0, ratio));
  const start = channels(theme.gradientStart);
  const end = channels(theme.gradientEnd);
  const hex = start.map((from, index) => {
    const value = Math.round(from + (end[index] - from) * clamped);
    return value.toString(16).padStart(2, "0");
  });
  return `#${hex.join("")}`;
}

function channels(hex: string): [number, number, number] {
  return [1, 3, 5].map((offset) => parseInt(hex.slice(offset, offset + 2), 16)) as [
    number,
    number,
    number,
  ];
}
