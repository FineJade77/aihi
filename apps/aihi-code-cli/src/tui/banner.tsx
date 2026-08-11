import { Box, Text } from "ink";

/** "AI-HI!" in block capitals; every line is padded to BANNER_WIDTH by render. */
const LOGO_LINES = [
  " █████╗ ██╗      ██╗  ██╗██╗██╗",
  "██╔══██╗██║      ██║  ██║██║██║",
  "███████║██║█████╗███████║██║██║",
  "██╔══██║██║╚════╝██╔══██║██║╚═╝",
  "██║  ██║██║      ██║  ██║██║██╗",
  "╚═╝  ╚═╝╚═╝      ╚═╝  ╚═╝╚═╝╚═╝",
] as const;

const BANNER_WIDTH = Math.max(...LOGO_LINES.map((line) => [...line].length));

const GRADIENT_START = [0x6d, 0x28, 0xd9] as const;
const GRADIENT_END = [0xc0, 0x84, 0xfc] as const;

/** Interpolates the purple ramp; chalk downgrades hex on non-truecolor terminals. */
export function gradientColor(ratio: number): string {
  const clamped = Math.min(1, Math.max(0, ratio));
  const hex = GRADIENT_START.map((start, index) => {
    const value = Math.round(start + (GRADIENT_END[index] - start) * clamped);
    return value.toString(16).padStart(2, "0");
  });
  return `#${hex.join("")}`;
}

/** Paints one string left-to-right across the ramp, one Text node per character. */
export function GradientText({ children, bold }: { children: string; bold?: boolean }) {
  const characters = [...children];
  const span = characters.length > 1 ? characters.length - 1 : 1;
  return (
    <Box>
      {characters.map((character, index) => (
        <Text key={index} bold={bold} color={gradientColor(index / span)}>
          {character}
        </Text>
      ))}
    </Box>
  );
}

/**
 * The startup wordmark. The ramp is keyed to each glyph's column in the full
 * banner, not its offset within its own line, so the gradient stays vertical.
 */
export function Banner() {
  const span = BANNER_WIDTH > 1 ? BANNER_WIDTH - 1 : 1;
  return (
    <Box flexDirection="column">
      {LOGO_LINES.map((line, row) => (
        <Box key={row}>
          {[...line].map((character, column) => (
            <Text key={column} color={gradientColor(column / span)}>
              {character}
            </Text>
          ))}
        </Box>
      ))}
    </Box>
  );
}
