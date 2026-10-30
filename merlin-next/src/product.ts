import { homedir } from "node:os";
import { join } from "node:path";

export const product = {
  name: "merlin-next",
  version: "0.1.0",
  bundleId: "ai.merlin.next",
  releaseChannel: "next",
} as const;

export function runtimePaths(home = homedir()) {
  const state = join(home, ".merlin-next");

  return {
    config: join(state, "config"),
    data: join(home, ".local", "share", "merlin-next"),
    cache: join(home, ".cache", "merlin-next"),
    logs: join(state, "logs"),
    runs: join(state, "runs"),
    sockets: join(state, "run"),
    browserProfiles: join(state, "browser-profiles"),
  } as const;
}
