// herdlet <-> opencode bridge.
//
// opencode has no shell-hook config like Claude Code / Codex, but it does have
// a plugin system. This plugin maps opencode's lifecycle events to `herdlet
// report` calls, so an opencode agent shows up in `herdlet list` and can be
// waited on / resumed just like a claude or codex worker.
//
// Install: drop this file in ~/.config/opencode/plugins/ (or run
// `herdlet setup`, which does it for you). opencode auto-loads it at startup.
//
// The agent id and pane come from the environment the opencode process was
// launched in: set HERDLET_ID (recommended) and/or run inside tmux so
// TMUX_PANE is present - herdlet's `report` resolves both. Every call is
// best-effort and swallowed: a coordination hiccup must never disrupt the agent.

export const HerdletPlugin = async ({ $ }) => {
  let lastSession = "";

  const report = async (state, extra = {}) => {
    const args = ["report", "--agent", "opencode", "--state", state];
    if (lastSession) args.push("--session", lastSession);
    if (extra.message !== undefined) args.push("--message", extra.message);
    try {
      // herdlet reads HERDLET_ID / TMUX_PANE from the inherited env.
      await $`herdlet ${args}`.quiet().nothrow();
    } catch {
      // never let coordination failure surface into the agent
    }
  };

  const remember = event => {
    const sid = event?.properties?.sessionID || event?.properties?.info?.id;
    if (sid) lastSession = String(sid);
  };

  return {
    event: async ({ event }) => {
      remember(event);
      switch (event.type) {
        case "session.status": {
          const kind = event.properties?.status?.type;
          if (kind === "busy") return report("working");
          if (kind === "idle") return report("done");
          return;
        }
        case "session.idle": // deprecated alias of session.status idle
          return report("done");
        case "tool.execute.before": // keep the record fresh through long tool runs
          return report("working");
        case "permission.asked":
          return report("blocked", { message: "awaiting approval" });
        case "permission.replied":
          return report("working");
        case "session.deleted": // terminal: keep the record + session ref for resume
          return report("ended");
        default:
          return;
      }
    },
    "permission.ask": async () => {
      await report("blocked", { message: "awaiting approval" });
    },
    dispose: async () => {
      await report("ended");
    },
  };
};
