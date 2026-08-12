import assert from "node:assert/strict";
import test from "node:test";
import { createTheme, gradientColor, resolveThemeName } from "../dist/tui/theme.js";

/** WCAG relative luminance, so the palette's claims are checked, not asserted. */
function luminance(hex) {
  const channels = [1, 3, 5]
    .map((offset) => parseInt(hex.slice(offset, offset + 2), 16) / 255)
    .map((value) => (value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4));
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrast(a, b) {
  const [lighter, darker] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (lighter + 0.05) / (darker + 0.05);
}

test("theme resolution prefers AIHI_THEME, then COLORFGBG, then dark", () => {
  assert.equal(resolveThemeName({ AIHI_THEME: "light" }), "light");
  assert.equal(resolveThemeName({ AIHI_THEME: " DARK ", COLORFGBG: "0;15" }), "dark");
  assert.equal(resolveThemeName({ COLORFGBG: "0;15" }), "light");
  assert.equal(resolveThemeName({ COLORFGBG: "0;7" }), "light");
  assert.equal(resolveThemeName({ COLORFGBG: "15;0" }), "dark");
  assert.equal(resolveThemeName({ COLORFGBG: "15;8" }), "dark");
  // Transparent backgrounds report "default"; assume the common dark terminal.
  assert.equal(resolveThemeName({ COLORFGBG: "15;default" }), "dark");
  assert.equal(resolveThemeName({}), "dark");
});

test("every palette tone stays legible against its own backgrounds", () => {
  const cases = [
    { theme: createTheme("dark"), backgrounds: ["#0d1117", "#1e1e1e", "#282c34"] },
    { theme: createTheme("light"), backgrounds: ["#ffffff", "#fdf6e3", "#f5f5f5"] },
  ];
  for (const { theme, backgrounds } of cases) {
    for (const background of backgrounds) {
      for (const tone of ["muted", "brand", "accent", "good", "warn", "bad"]) {
        const ratio = contrast(theme[tone], background);
        assert.ok(
          ratio >= 4.5,
          `${theme.name} ${tone} (${theme[tone]}) on ${background} is ${ratio.toFixed(2)}:1`,
        );
      }
      // Chrome and one-off hints only owe the 3:1 non-text threshold.
      for (const tone of ["faint", "border", "gradientStart", "gradientEnd"]) {
        const ratio = contrast(theme[tone], background);
        assert.ok(
          ratio >= 3,
          `${theme.name} ${tone} (${theme[tone]}) on ${background} is ${ratio.toFixed(2)}:1`,
        );
      }
    }
  }
});

test("the wordmark ramp interpolates between the palette's two purples", () => {
  const theme = createTheme("dark");
  assert.equal(gradientColor(theme, 0), theme.gradientStart);
  assert.equal(gradientColor(theme, 1), theme.gradientEnd);
  // Out-of-range ratios clamp rather than producing invalid hex.
  assert.equal(gradientColor(theme, -1), theme.gradientStart);
  assert.equal(gradientColor(theme, 2), theme.gradientEnd);
  assert.match(gradientColor(theme, 0.5), /^#[0-9a-f]{6}$/);
});
