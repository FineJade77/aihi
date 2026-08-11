import * as process from "node:process";
import { render } from "ink";
import { TuiApp, type TuiAppProps } from "./app.js";

export async function runTui(options: TuiAppProps): Promise<void> {
  if (!process.stdin.isTTY || !process.stdout.isTTY) {
    process.stderr.write("aihi-code requires an interactive terminal (TTY).\n");
    return;
  }
  const application = render(<TuiApp {...options} />);
  await application.waitUntilExit();
}

export { TuiApp } from "./app.js";
export type { TuiAppProps } from "./app.js";
