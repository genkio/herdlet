// herdlet <-> pi bridge.
//
// pi has no shell-hook config like Claude Code / Codex, but it does have an
// extension system. This extension maps pi's lifecycle events to `herdlet hook`
// calls, so a pi agent shows up in `herdlet list` and can be waited on, peeked
// at (`--transcript`) and resumed just like a claude or codex worker.
//
// Install: drop this file in ~/.pi/agent/extensions/ (or run `herdlet setup`,
// which does it for you). pi auto-loads it at startup.
//
// The agent id and pane come from the environment the pi process was launched
// in: set HERDLET_ID (recommended) and/or run inside tmux so TMUX_PANE is
// present - `herdlet hook` resolves both. Every call is fire-and-forget and
// swallowed: a coordination hiccup must never disrupt the agent.

// @ts-nocheck

import { spawn } from "node:child_process";

const ENABLED = Boolean(process.env.HERDLET_ID || process.env.TMUX_PANE);

function hook(event, ctx, extra = {}) {
  try {
    const payload = { hook_event_name: event, cwd: ctx?.cwd, ...extra };
    try {
      payload.session_id = ctx?.sessionManager?.getSessionId?.();
      payload.transcript_path = ctx?.sessionManager?.getSessionFile?.();
    } catch {
      // a session ref is a bonus; the state report matters more
    }
    const child = spawn("herdlet", ["hook", "--agent", "pi", "--event", event], {
      detached: true,
      stdio: ["pipe", "ignore", "ignore"],
    });
    child.on("error", () => {});       // ENOENT: herdlet is not installed
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(payload));
    child.unref();
  } catch {
    // never let coordination failure surface into the agent
  }
}

export default function (pi) {
  if (!ENABLED) {
    return;
  }

  let active = false;

  pi.on("session_start", (_event, ctx) => {
    // TUI only: print/json/rpc modes have no pane for herdlet to address.
    active = ctx?.mode === "tui";
    if (active) {
      hook("SessionStart", ctx);
    }
  });

  pi.on("before_agent_start", (event, ctx) => {
    if (active) {
      hook("UserPromptSubmit", ctx, { prompt: event?.prompt });
    }
  });

  pi.on("tool_execution_start", (_event, ctx) => {
    if (active) {
      hook("PreToolUse", ctx);
    }
  });

  pi.on("ui_prompt_start", (event, ctx) => {
    if (active) {
      hook("PermissionRequest", ctx, {
        message: event?.title || event?.label || "awaiting input",
      });
    }
  });

  pi.on("ui_prompt_end", (_event, ctx) => {
    if (active) {
      hook("PreToolUse", ctx);
    }
  });

  pi.on("agent_settled", (_event, ctx) => {
    // pi may still auto-retry or drain queued messages; only a settled idle
    // agent is really done.
    if (active && ctx?.isIdle?.() === true) {
      hook("Stop", ctx);
    }
  });

  pi.on("session_before_compact", (_event, ctx) => {
    if (active) {
      hook("PreCompact", ctx);
    }
  });

  pi.on("session_shutdown", (_event, ctx) => {
    if (active) {
      hook("SessionEnd", ctx);
    }
  });
}
