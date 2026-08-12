import { Box, Text } from "ink";
import { gradientColor, useTheme } from "./theme.js";

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

/** Paints one string left-to-right across the ramp, one Text node per character. */
export function GradientText({ children, bold }: { children: string; bold?: boolean }) {
  const theme = useTheme();
  const characters = [...children];
  const span = characters.length > 1 ? characters.length - 1 : 1;
  return (
    <Box>
      {characters.map((character, index) => (
        <Text key={index} bold={bold} color={gradientColor(theme, index / span)}>
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
  const theme = useTheme();
  const span = BANNER_WIDTH > 1 ? BANNER_WIDTH - 1 : 1;
  return (
    <Box flexDirection="column">
      {LOGO_LINES.map((line, row) => (
        <Box key={row}>
          {[...line].map((character, column) => (
            <Text key={column} color={gradientColor(theme, column / span)}>
              {character}
            </Text>
          ))}
        </Box>
      ))}
    </Box>
  );
}
