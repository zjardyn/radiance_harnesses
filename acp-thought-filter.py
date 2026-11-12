#!/usr/bin/env python3
"""ACP stdio relay with a live three-state thinking display for Zed's Agent Panel.

Zed spawns ACP agents over stdio (newline-delimited JSON-RPC). This relay sits in
the middle, launches the real agent, and presents `agent_thought_chunk` /
`agent_thought` updates according to a mode file. Everything else — user and
assistant messages, tool calls and output, plans, config options, usage,
requests, responses — passes through untouched.

Modes (file content, one word; read live, ~0.25s cache, switchable mid-session):
  off          (picker label "Low") minimal panel: drop thinking text, and
               fold ALL tool activity (thinking, edits, commands, reads...) into
               ONE static card whose title names the current state - "Thinking",
               "Editing file", "Running command" - updated in place, closed at
               turn end.
  breadcrumbs  collapse each streamed reasoning segment into one short milestone
               line, emitted when the segment ends (next non-thought event or
               turn end). Pure local text digest: no extra inference, no latency.
  detailed     pass thoughts through byte-for-byte (Zed shows the full stream)

Presentation only: the agent still reasons; OpenCode still receives, stores, and
replays the reasoning in its own session context. Reasoning effort, generation
limits, and inference are untouched.

The relay also advertises one ACP config option, "Thinking", so the mode is a
picker in Zed's composer next to Model / Effort / System prompt, and serves
picks of it locally (see OPTION_ID). Kill switch: `echo off >
~/.config/zed/thinking-option` disables the picker and restores byte-for-byte
passthrough of that request.

Usage:  acp-thought-filter.py [--keep] [--mode-file PATH] [-- <child> <args...>]
  --keep        shorthand for detailed mode (backwards compatible)
  --mode-file   override the mode file path

With no child command, the newest OpenCode binary in Zed's external-agent
registry is used with the `acp` subcommand.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import threading
import time

THOUGHT_TOKEN = b"agent_thought"
CHILD_METHOD = "opencode/session/child_update"  # OpenCode ext-notification: subagent/historian child streams
CHILD_META = "opencode/child-session"           # _meta marker on projected child session/update
CHILD_METHOD_B = CHILD_METHOD.encode()
CHILD_META_B = CHILD_META.encode()
# Subagent card translation is OFF by default: the known-good baseline drops all
# child traffic, because Zed has no handler for child-session protocol messages.
# Set ZED_RELAY_SUBAGENTS=1 to translate subagent tool activity into plain
# tool_call/tool_call_update cards on the parent thread (internal Magic Context
# carriers stay dropped, keyed on the child-session title in `_meta`).
SUBAGENTS_ON = os.environ.get("ZED_RELAY_SUBAGENTS", "0") == "1"
DEFAULT_MODE_FILE = os.path.expanduser("~/.config/zed/thinking-mode")
DEFAULT_MODE = "breadcrumbs"
MODES = ("off", "breadcrumbs", "detailed")
LOG_PATH = os.path.expanduser("~/.cache/zed-acp-filter.log")
# Outbound messages between counter snapshots (a session writes ~50-200 per turn).
LOG_EVERY = int(os.environ.get("ZED_RELAY_LOG_EVERY", "100"))

DIGEST_BUDGET = 160
SEGMENT_CAP = 8000  # chars of raw thought kept for digesting
CARD_BUDGET = 700   # max chars of accumulated milestone log shown in the card
CARD_LINES = 8      # max milestone lines kept in the card
KEY_PREFIX = "zed-acp-relay:activity:"  # stable keyed messageId namespace
TOOL_CALL_B = b'"tool_call"'
# off ("Low") mode: every tool activity updates ONE static card instead of
# painting its own row. Title = the current state, named per tool kind.
LOW_KIND_LABELS = {"read": "Reading", "search": "Searching", "fetch": "Fetching",
                   "edit": "Editing", "delete": "Deleting", "move": "Moving",
                   "execute": "Running", "think": "Thinking"}
LOW_DONE_LABELS = {"read": "Reads done", "search": "Searches done", "fetch": "Fetches done",
                   "edit": "Edits done", "delete": "Deletions done", "move": "Moves done",
                   "execute": "Commands done", "think": "Thought done"}
KIND_B = b'"kind"'
STOP_TOKEN = b'"stopReason"'  # prompt response: the turn is over
TOOL_UPD_B = b'"tool_call_update"'
TOOL_ID_B = b'"toolCallId"'  # every card line carries it; gates the Low-mode parse
TITLES_ON = os.environ.get("ZED_RELAY_TITLES", "on") != "off"
TITLE_BUDGET = 60
# Display-only cap for tool output text inside the expanded card. Zed renders
# exactly what the agent sends, so a very long log makes the card a wall of text.
# The cap never touches the tool result the model sees (that stays agent-side);
# it is opt-out: ZED_RELAY_TOOL_LINES=0 keeps output byte-for-byte.
TOOL_OUT_LINES = int(os.environ.get("ZED_RELAY_TOOL_LINES", "120"))
TOOL_OUT_BYTES = int(os.environ.get("ZED_RELAY_TOOL_BYTES", "12000"))

# --- the "Thinking" composer option (ACP configOptions) ----------------------
# Zed renders every entry of a session's `configOptions` as a picker at the
# bottom of the Agent Panel and answers a pick with `session/set_config_option`.
# OpenCode already drives that channel (Model / Effort / Session Mode / System
# prompt), so the relay appends ONE option of its own and serves it locally: the
# picker is the same knob as the mode file, no keybinding involved. Field names
# copy OpenCode's own option verbatim ({id, name, description, category,
# type:"select", currentValue, options:[{value, name}]}), and the category is
# OpenCode's "thought_level" — a category Zed demonstrably renders, which is the
# safe choice over inventing a new one. Kill switch, no code edit and no reboot:
#   echo off > ~/.config/zed/thinking-option   (see ~/zed-tools/relay_reset.sh)
OPTION_ID = "thinking_display"
OPTION_CATEGORY = os.environ.get("ZED_RELAY_OPTION_CATEGORY", "thought_level")
OPTION_LABELS = {"breadcrumbs": "Breadcrumbs", "detailed": "Detailed", "off": "Low"}
OPTION_DESCS = {
    "breadcrumbs": "One milestone line per reasoning segment",
    "detailed": "Stream the full reasoning",
    "off": "Minimal: one static Thinking row, no text",
}
OPTION_ORDER = ("breadcrumbs", "detailed", "off")
OPTION_FLAG = os.environ.get("ZED_THINKING_OPTION_FILE", os.path.expanduser("~/.config/zed/thinking-option"))
SET_CONFIG_B = b'"session/set_config_option"'
CONFIG_UPDATE_B = b'"config_option_update"'
SESSION_ID_B = b'"sessionId"'
RESULT_B = b'"result"'
# stdout is written from two threads once the relay answers requests itself.
STDOUT_LOCK = threading.Lock()
# Guards the announced-mode check-and-set shared by pump_out and the watchdog.
MODE_LOCK = threading.Lock()


def dumps_line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def send(dst, data: bytes) -> None:
    """The only writer to Zed's stdout: serialises pump_out with the answers
    pump_raw generates for our own config option."""
    with STDOUT_LOCK:
        dst.write(data)
        dst.flush()


def option_enabled() -> bool:
    """Flag file decides; absent file means enabled, so a machine without one
    behaves exactly as before. Read per use: it is one tiny file, and the whole
    point is that recovery needs no relay restart."""
    try:
        with open(OPTION_FLAG, encoding="utf-8") as fh:
            return fh.read().strip().lower() not in ("off", "0", "no", "false")
    except OSError:
        return True


def thinking_option(mode_name: str) -> dict:
    current = mode_name if mode_name in MODES else DEFAULT_MODE
    return {
        "id": OPTION_ID,
        "name": "Thinking",
        "description": "How much of my reasoning this panel shows (display only)",
        "category": OPTION_CATEGORY,
        "type": "select",
        "currentValue": current,
        "options": [{"value": v, "name": OPTION_LABELS[v], "description": OPTION_DESCS[v]}
                    for v in OPTION_ORDER],
    }


def base_options(state: dict, sid, mode_name: str) -> list:
    """The agent's own options (last seen) plus ours — Zed replaces the whole
    list on every config_option_update, so an update that omits theirs would
    erase the Model/Effort/System-prompt pickers."""
    agent = state["options"].get(sid, []) if isinstance(sid, str) else []
    return list(agent) + [thinking_option(mode_name)]


def default_child() -> list[str]:
    root = os.path.expanduser(
        "~/.local/share/zed/external_agents/registry/opencode"
    )
    candidates = sorted(
        glob.glob(os.path.join(root, "v_*", "opencode")),
        key=os.path.getmtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit(f"acp-thought-filter: no OpenCode binary under {root}")
    return [candidates[0], "acp"]


class ModeFile:
    """Mode read live from disk with a small time cache."""

    def __init__(self, path: str, fallback: str) -> None:
        self.path = path
        self.fallback = fallback if fallback in MODES else DEFAULT_MODE
        self._lock = threading.Lock()
        self._mode = self._read()
        self._checked = time.monotonic()

    def _read(self) -> str:
        try:
            with open(self.path, encoding="utf-8") as fh:
                mode = fh.read().strip()
        except OSError:
            return self.fallback
        if mode == "keep":
            mode = "detailed"
        return mode if mode in MODES else self.fallback

    def get(self) -> str:
        now = time.monotonic()
        if now - self._checked >= 0.25:
            self._checked = now
            self._mode = self._read()
        return self._mode

    def apply(self, value: str) -> None:
        """Set the mode authoritatively: write the file AND refresh the cache.
        Without the cache refresh, a pick served by pump_raw races the watchdog,
        which reads its stale cached value and announces it to Zed's picker."""
        with self._lock:
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(value)
            self._mode = value
            self._checked = time.monotonic()


def digest(text: str) -> str:
    """One-line, human-readable milestone from a raw reasoning segment."""
    text = " ".join(text.split())
    if not text:
        return ""
    if len(text) <= DIGEST_BUDGET:
        return text
    cut = text[: DIGEST_BUDGET + 1]
    # Prefer ending on a sentence boundary, then on a word boundary.
    for end in (". ", "! ", "? "):
        i = cut.rfind(end)
        if 20 <= i:
            return cut[: i + 1].rstrip()
    i = cut.rfind(" ")
    if i > 20:
        cut = cut[:i]
    return cut[: DIGEST_BUDGET - 1].rstrip(",;: ") + "…"


def is_internal_title(title) -> bool:
    """Magic Context's hidden carriers (historian `magic-context-compartment`,
    dreamer `magic-context-dream-*`, "Magic Context") — their payloads are internal
    protocol text, never panel content. Real subagents are titled by the task tool
    with the caller's description, so they fall through."""
    low = (title or "").lower()
    return "magic-context" in low or "magic context" in low


def child_root(state, sid, depth=0):
    """Root session a child reports into (updates must be addressed to it: Zed
    routes by sessionId and has no thread for the child's own id)."""
    while depth < 8:
        parent = state["child_parent"].get(sid)
        if parent is None:
            return sid if sid in state["turns"] else None
        sid = parent
        depth += 1
    return None


# Child updates that would damage the parent's transcript if re-addressed to it.
CHILD_DROP_KINDS = {
    "plan", "plan_update", "plan_removed", "session_info_update", "current_mode_update",
    "available_commands_update", "config_option_update", "usage_update", "notice",
    "agent_message_chunk", "agent_thought", "user_message_chunk",
}


def handle_child(msg: dict, mode: str, state: dict, counters: dict) -> bytes | None:
    """Rewrite legitimate subagent traffic into the parent thread; drop internal
    carriers. Returns the line to forward, or None to drop."""
    params = msg.get("params")
    if not isinstance(params, dict):
        return None
    title = None
    if msg.get("method") == CHILD_METHOD:
        child = params.get("childSessionId")
        root = params.get("rootSessionId")
        title = params.get("title")
        if params.get("type") != "update":
            return None
        update = params.get("update")
        if isinstance(child, str) and isinstance(root, str):
            state["child_parent"][child] = root
    else:
        update = params.get("update")
        meta = update.get("_meta") if isinstance(update, dict) else None
        cmeta = meta.get(CHILD_META) if isinstance(meta, dict) else None
        if not isinstance(cmeta, dict):
            return None
        child = cmeta.get("id")
        title = cmeta.get("title")
        root = cmeta.get("parentID")
        if isinstance(child, str) and isinstance(root, str):
            state["child_parent"][child] = root
    if not isinstance(child, str) or not isinstance(update, dict):
        return None
    # Classified on child-session metadata (`_meta['opencode/child-session'].title`),
    # never on payload text: no payload content is ever inspected for forwarding.
    if not isinstance(title, str) or is_internal_title(title) or not SUBAGENTS_ON:
        return None
    target = child_root(state, child)
    if target is None:
        return None
    kind = update.get("sessionUpdate")
    if kind in CHILD_DROP_KINDS:
        return None
    if kind == "agent_thought_chunk" and mode != "detailed":
        return None
    update = dict(update)
    if kind in ("tool_call", "tool_call_update"):
        tid = update.get("toolCallId")
        if isinstance(tid, str) and not tid.startswith(child + ":"):
            update["toolCallId"] = f"{child}:{tid}"  # avoid id collisions with the parent
        text = update.get("title")
        if isinstance(text, str) and isinstance(title, str) and title and not text.startswith(title):
            update["title"] = (f"{title}: {text}")[: TITLE_BUDGET + 12]
    counters["child_forwarded"] = counters.get("child_forwarded", 0) + 1
    line = {"jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": target, "update": update}}
    return (json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def is_child_update(msg: dict) -> bool:
    """True for OpenCode's internal child-session traffic (subagents, Magic Context
    historian/dreamer carriers). Its raw protocol payloads (compartment XML,
    internal prompts) are not for the Agent Panel; dropping them is safe —
    child work is persisted in OpenCode and reached via ctx_search, not ACP."""
    if msg.get("method") == CHILD_METHOD:
        return True
    params = msg.get("params")
    if isinstance(params, dict):
        update = params.get("update")
        if isinstance(update, dict):
            meta = update.get("_meta")
            if isinstance(meta, dict) and CHILD_META in meta:
                return True
    return False


def thought_update(msg: dict) -> tuple[str, str] | None:
    """(session_id, text) for a thought session/update, else None."""
    if "id" in msg or msg.get("method") not in ("session/update", "session/notification"):
        return None
    params = msg.get("params")
    if not isinstance(params, dict):
        return None
    update = params.get("update")
    if not isinstance(update, dict):
        return None
    kind = update.get("sessionUpdate")
    session_id = params.get("sessionId")
    if not isinstance(session_id, str):
        return None
    if kind == "agent_thought_chunk":
        content = update.get("content")
        if isinstance(content, dict) and content.get("type") == "text":
            text = content.get("text")
            if isinstance(text, str):
                return session_id, text
        return None
    if kind == "agent_thought":  # whole-thought upsert variant
        text = update.get("text")
        if not isinstance(text, str):
            content = update.get("content")
            text = content.get("text") if isinstance(content, dict) else ""
        return session_id, text or ""
    return None


def _basename(value: str) -> str:
    value = value.rstrip("/")
    return value.rsplit("/", 1)[-1] if "/" in value else value


def _field(update: dict, raw: dict, *keys: str) -> str:
    for src in (raw, update):
        for key in keys:
            value = src.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def short_title(update: dict) -> str | None:
    """One-line human summary of a tool call, e.g. 'Reading AGENTS.md'.

    Only the display `title` is rewritten: toolCallId, status, kind, rawInput,
    content (commands, diffs, logs) and locations all pass through, so the
    expanded card keeps every detail. Returns None when nothing better than the
    agent's own title can be derived (keep it)."""
    kind = update.get("kind")
    raw = update.get("rawInput") if isinstance(update.get("rawInput"), dict) else {}
    title = _field(update, raw, "title")
    locs = update.get("locations")
    path = ""
    if isinstance(locs, list):
        for loc in locs:
            if isinstance(loc, dict) and isinstance(loc.get("path"), str) and loc["path"]:
                path = loc["path"]
                break
    verb = {
        "read": "Reading",
        "edit": "Editing",
        "delete": "Deleting",
        "move": "Moving",
        "search": "Searching",
        "execute": "Running",
        "fetch": "Fetching",
        "think": "Thinking",
    }.get(kind if isinstance(kind, str) else "")
    if verb is None:
        return None
    if verb in ("Reading", "Editing", "Deleting", "Moving"):
        target = _basename(path) if path else _basename(title)
        return f"{verb} {target}" if target else None
    if verb == "Running":
        cmd = _field(raw, update, "command", "cmd")
        if not cmd:
            return None  # no command on this line: caller reuses the cached summary
        cmd = " ".join(cmd.split())
        return f"Running {cmd[:TITLE_BUDGET - 9] + '…' if len(cmd) > TITLE_BUDGET - 9 else cmd}"
    if verb == "Searching":
        q = _field(raw, update, "query", "pattern", "glob", "path")
        return f"Searching {q[:TITLE_BUDGET - 10]}" if q else None
    if verb == "Fetching":
        url = _field(raw, update, "url")
        host = url.split("//", 1)[-1].split("/", 1)[0] if url else ""
        return f"Fetching {host}" if host else None
    return verb


def low_activity_line(msg: dict) -> tuple[str, str, str, str] | None:
    """(session_id, tool_call_id, status, state label) for any tool card line in
    Low mode. The label names the current activity - "Thinking", "Editing
    file.py", "Running cmd" (generic verb when fields are missing). OpenCode
    stamps `kind` on the open line and the updates, so a tool's first line
    announces its state."""
    params = msg.get("params")
    if not isinstance(params, dict):
        return None
    update = params.get("update")
    if not isinstance(update, dict):
        return None
    if update.get("sessionUpdate") not in ("tool_call", "tool_call_update"):
        return None
    sid = params.get("sessionId")
    tcid = update.get("toolCallId")
    if not isinstance(sid, str) or not isinstance(tcid, str):
        return None
    kind = update.get("kind")
    status = update.get("status")
    status = status if isinstance(status, str) else "in_progress"
    label = LOW_KIND_LABELS.get(kind if isinstance(kind, str) else "")
    if label is None:
        title = update.get("title")
        label = title.strip()[: TITLE_BUDGET] if isinstance(title, str) and title.strip() \
            else "Working"
        return sid, tcid, status, label
    if status == "completed":
        label = LOW_DONE_LABELS.get(kind, "Done")
    elif status == "failed":
        label = "\u2717 " + LOW_DONE_LABELS.get(kind, "Done")
    else:
        raw = update.get("rawInput") if isinstance(update.get("rawInput"), dict) else {}
        locs = update.get("locations")
        path = ""
        if isinstance(locs, list):
            for loc in locs:
                if isinstance(loc, dict) and isinstance(loc.get("path"), str) and loc["path"]:
                    path = loc["path"]
                    break
        if label in ("Reading", "Editing", "Deleting", "Moving"):
            target = _basename(path) if path else _basename(
                update.get("title") if isinstance(update.get("title"), str) else "")
            label = f"{label} {target}".strip()
        elif label == "Running":
            cmd = _field(update, raw, "command", "cmd")
            cmd = " ".join(cmd.split())
            label = (f"Running {cmd[: TITLE_BUDGET - 9]}"
                     + ("\u2026" if len(cmd) > TITLE_BUDGET - 9 else "")) if cmd \
                else "Running command"
        elif label in ("Searching", "Fetching"):
            q = _field(update, raw, "query", "pattern", "glob", "path", "url")
            label = f"{label} {q[: TITLE_BUDGET - len(label) - 1]}".strip() if q else label
    return sid, tcid, status, label


def cap_tool_output(update: dict) -> bool:
    """Trim very long tool output text for display; keeps head and tail lines."""
    if TOOL_OUT_LINES <= 0 and TOOL_OUT_BYTES <= 0:
        return False
    items = update.get("content")
    if not isinstance(items, list):
        return False
    changed = False
    for item in items:
        if not isinstance(item, dict):
            continue
        inner = item.get("content") if item.get("type") == "content" else item
        if not isinstance(inner, dict) or inner.get("type") != "text":
            continue
        text = inner.get("text")
        if not isinstance(text, str):
            continue
        lines = text.splitlines()
        if len(lines) <= TOOL_OUT_LINES and len(text) <= TOOL_OUT_BYTES:
            continue
        head = lines[: TOOL_OUT_LINES // 2]
        tail = lines[-(TOOL_OUT_LINES - len(head)) :]
        omitted = len(lines) - len(head) - len(tail)
        inner["text"] = "\n".join(head + [f"\n… {omitted} lines omitted (full output stays in OpenCode)\n"] + tail)
        changed = True
    return changed


def rewrite_tool_line(msg: dict, cache: dict | None = None) -> bytes | None:
    """Shorten tool card titles in place; None if the message needs no change.

    `cache` remembers the summary per (sessionId, toolCallId) because OpenCode's
    `tool_call_update` lines often carry only the raw title - Zed keys the card by
    toolCallId, so reusing the first summary keeps one clean line per tool."""
    params = msg.get("params")
    if not isinstance(params, dict):
        return None
    update = params.get("update")
    if not isinstance(update, dict):
        return None
    if update.get("sessionUpdate") not in ("tool_call", "tool_call_update"):
        return None
    sid = params.get("sessionId")
    tcid = update.get("toolCallId")
    key = (sid, tcid) if isinstance(sid, str) and isinstance(tcid, str) else None
    title = short_title(update)
    if not title and cache and key in cache:
        title = cache[key]
    if not title:
        return None
    if cache is not None and key is not None:
        cache[key] = title
    if update.get("title") == title:
        return None
    update["title"] = title
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def synth_thought(session_id: str, text: str) -> bytes:
    """Legacy append-style thought chunk (used by pre-keyed builds/fallback)."""
    line = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": f"• {text}"},
            },
        },
    }
    return (json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def synth_keyed_thought(session_id: str, message_id: str, text: str) -> bytes:
    """Keyed whole-thought upsert (v2 protocol only — Zed's v1 wire drops it, see
    acp_thread::handle_session_update's `_ => {}`). Kept behind
    ZED_RELAY_KEYED_THOUGHT=1 for v2 builds: Zed replaces the thought with this id
    IN PLACE at its first-seen position."""
    line = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_thought",
                "messageId": message_id,
                "content": [{"type": "text", "text": text}],
            },
        },
    }
    return (json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _tool_update(card_id: str, status: str, title: str, body: str | None) -> dict:
    update: dict = {
        "sessionUpdate": "tool_call",
        "toolCallId": card_id,
        "kind": "think",
        "status": status,
        "title": title,
        "content": [],
    }
    if body is not None:
        update["content"] = [
            {"type": "content", "content": {"type": "text", "text": body}}
        ]
    return update


def synth_activity(session_id: str, card_id: str, title: str, body: str, first: bool) -> bytes:
    """One live activity indicator, v1-native.

    Zed keys tool cards by toolCallId and updates them IN PLACE at their original
    position (`update_tool_call` -> `index_for_tool_call`; content is cleared then
    replaced), so a single synthetic card can be rewritten all turn without adding
    rows, interleaved tool calls don't split it, and it renders collapsed by
    default — the detail lives in the expandable body. `kind: think` marks it as
    reasoning, so it stays out of the permission path."""
    update = _tool_update(card_id, "in_progress", title, body)
    if not first:
        update["sessionUpdate"] = "tool_call_update"
        update.pop("kind", None)
    return (
        json.dumps(
            {"jsonrpc": "2.0", "method": "session/update",
             "params": {"sessionId": session_id, "update": update}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def synth_activity_done(session_id: str, card_id: str, steps: int) -> bytes:
    update = _tool_update(card_id, "completed", f"Thought {steps} steps", None)
    update["sessionUpdate"] = "tool_call_update"
    update.pop("kind", None)
    update["content"] = []
    return (
        json.dumps(
            {"jsonrpc": "2.0", "method": "session/update",
             "params": {"sessionId": session_id, "update": update}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def parse_args(argv: list[str]) -> tuple[str | None, str, list[str]]:
    """Returns (forced_mode_from_keep, mode_file, child_cmd)."""
    forced = None
    if "--keep" in argv:
        forced = "detailed"
        argv = [a for a in argv if a != "--keep"]
    mode_file = os.environ.get("ZED_THINKING_MODE_FILE", DEFAULT_MODE_FILE)
    if "--mode-file" in argv:
        i = argv.index("--mode-file")
        if i + 1 < len(argv):
            mode_file = argv[i + 1]
            del argv[i : i + 2]
    if argv and argv[0] == "--":
        argv = argv[1:]
    return forced, mode_file, (argv or default_child())


def log_counters(counters: dict, mode_name: str, tag: str, child: str = "") -> None:
    """Append a counter snapshot; 'live' lines let a check run during a session."""
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(
                f"{tag} pid={os.getpid()} {f'child={child} ' if child else ''}mode={mode_name} "
                f"passed={counters['passed']} dropped={counters['dropped']} "
                f"buffered={counters['buffered']} breadcrumbs={counters['breadcrumbs']} "
                f"low_folded={counters.get('low_folded', 0)} "
                f"child_dropped={counters['child_dropped']} "
                f"child_forwarded={counters.get('child_forwarded', 0)}\n"
            )
    except OSError:
        pass


def add_option(payload: bytes, mode_name: str, state: dict) -> bytes:
    """Append our option to a session/new|load|resume response. Anything that
    does not parse as such a response is forwarded byte-for-byte: a failed
    injection must never cost the agent its connection."""
    try:
        msg = json.loads(payload)
    except ValueError:
        return payload
    if not isinstance(msg, dict) or "id" not in msg:
        return payload
    res = msg.get("result")
    if not isinstance(res, dict) or not isinstance(res.get("sessionId"), str):
        return payload
    sid = res["sessionId"]
    opts = res.get("configOptions")
    agent = [o for o in opts if isinstance(o, dict) and o.get("id") != OPTION_ID] \
        if isinstance(opts, list) else []
    state["options"][sid] = agent
    if any(isinstance(o, dict) and o.get("id") == OPTION_ID for o in (opts or [])):
        return payload
    res["configOptions"] = agent + [thinking_option(mode_name)]
    return dumps_line(msg)


def merge_option_update(payload: bytes, mode_name: str, state: dict) -> bytes:
    """OpenCode rewrites the option list (effort/model picks): record theirs and
    re-append ours, so our picker survives their refresh."""
    try:
        msg = json.loads(payload)
    except ValueError:
        return payload
    if not isinstance(msg, dict) \
            or msg.get("method") not in ("session/update", "session/notification"):
        return payload
    params = msg.get("params")
    upd = params.get("update") if isinstance(params, dict) else None
    if not isinstance(upd, dict) or upd.get("sessionUpdate") != "config_option_update":
        return payload
    sid = params.get("sessionId")
    opts = upd.get("configOptions")
    agent = [o for o in opts if isinstance(o, dict) and o.get("id") != OPTION_ID] \
        if isinstance(opts, list) else []
    if isinstance(sid, str):
        state["options"][sid] = agent
    upd["configOptions"] = agent + [thinking_option(mode_name)]
    return dumps_line(msg)


def synth_config_update(sid: str, mode_name: str, state: dict) -> bytes:
    return dumps_line({"jsonrpc": "2.0", "method": "session/update",
                       "params": {"sessionId": sid,
                                  "update": {"sessionUpdate": "config_option_update",
                                             "configOptions": base_options(state, sid,
                                                                           mode_name)}}})


def option_answer(line: bytes, mode: ModeFile, counters: dict, state: dict) -> bytes | None:
    """Answer a pick of OUR option locally (the agent has no such configId, so
    forwarding it would return a method-level error and light up Zed).
    Returns the JSON-RPC response for Zed, or None when the request is the
    agent's own."""
    if SET_CONFIG_B not in line:
        return None
    if not option_enabled():
        return None  # option disabled: a pick of an id we never advertised is
                     # the agent's business, exactly as before the feature.
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None
    params = msg.get("params")
    if not isinstance(params, dict) or params.get("configId") != OPTION_ID:
        return None
    rid, sid, value = msg.get("id"), params.get("sessionId"), params.get("value")
    if value not in MODES:
        return dumps_line({"jsonrpc": "2.0", "id": rid, "error": {
            "code": -32602, "message": f"unknown {OPTION_ID} value"}})
    try:
        mode.apply(value)
    except OSError as exc:
        return dumps_line({"jsonrpc": "2.0", "id": rid,
                           "error": {"code": -32603, "message": f"mode file: {exc}"}})
    counters["option_sets"] = counters.get("option_sets", 0) + 1
    with MODE_LOCK:
        state["announced"] = value  # ours caused the change: no echo needed
    res = {"configOptions": base_options(state, sid, value)}
    if isinstance(sid, str):
        res["sessionId"] = sid
    return dumps_line({"jsonrpc": "2.0", "id": rid, "result": res})


def pump_out(src, dst, mode: ModeFile, counters: dict, state: dict) -> None:
    """child -> Zed: applies the display mode to thought updates.

    breadcrumbs: each reasoning segment becomes one short milestone line, and every
    milestone is published into a SINGLE keyed thought card (stable per-turn
    messageId, whole-thought upsert). Zed replaces that card in place at its
    first-seen position — interleaved tool calls cannot split it — so a turn shows
    one live activity log, not N 'Thinking' rows."""
    pending: dict[str, list[str]] = {}  # session_id -> buffered thought pieces
    pending_len = 0
    cards: dict[str, list[str]] = {}   # session_id -> milestone lines this turn
    last_sent: dict[str, str] = {}     # session_id -> last card text emitted
    opened: dict[str, str] = {}        # session_id -> id of the live activity card
    steps: dict[str, int] = {}         # session_id -> milestones shown this turn
    keyed = os.environ.get("ZED_RELAY_KEYED_THOUGHT") == "1"  # v2 builds only

    sent_once: set[str] = set()  # sessions whose activity card already exists
    # off ("Low") mode: native toolCallId -> the one static activity card, and
    # sessions whose card is open (closed at turn end).
    think_cards: dict[tuple[str, str], str] = {}
    think_open: set[str] = set()

    def card_id(sid: str) -> str:
        return f"{KEY_PREFIX}{sid}:t{state['turns'].get(sid, 0)}"

    def close_card(sid: str | None) -> None:
        """Finish the live activity card (no spinner left, final step count)."""
        if not isinstance(sid, str):
            return
        cid = opened.pop(sid, None)
        if cid is None:
            return
        send(dst, synth_activity_done(sid, cid, steps.pop(sid, 0)))

    seen_tools: dict[str, set[str]] = {}  # sid -> toolCallIds already in the card
    tool_titles: dict[tuple[str, str], str] = {}  # (sid, toolCallId) -> summary

    def emit(line: bytes, stripped: bytes, msg: dict | None, m: str) -> None:
        """Pass a non-thought line through: shorten tool titles (display only), and
        in breadcrumbs mode record finished tool calls as milestones in the activity
        card, then flush the card before the tool card is written."""
        out = line
        if TOOL_CALL_B in stripped or TOOL_UPD_B in stripped or TOOL_ID_B in stripped:
            if msg is None:
                try:
                    msg = json.loads(stripped)
                except ValueError:
                    msg = None
            if isinstance(msg, dict):
                params = msg.get("params")
                upd = params.get("update") if isinstance(params, dict) else None
                sid_t = params.get("sessionId") if isinstance(params, dict) else None
                if m == "off" and (KIND_B in stripped or TOOL_CALL_B in stripped
                                    or TOOL_UPD_B in stripped
                                    or TOOL_ID_B in stripped):
                    # Low mode: no per-tool rows at all. Every tool card feeds
                    # ONE static activity row ("Thinking", "Editing x.py",
                    # "Running cmd"), updated in place; the raw card is dropped.
                    # The agent's action itself is untouched - display only.
                    act = low_activity_line(msg)
                    if act is not None:
                        sid_a, tcid_a, status_a, label_a = act
                        if (sid_a, tcid_a) in think_cards:
                            send(dst, dumps_line({
                                "jsonrpc": "2.0", "method": "session/update",
                                "params": {"sessionId": sid_a, "update": {
                                    "sessionUpdate": "tool_call_update",
                                    "toolCallId": think_cards[(sid_a, tcid_a)],
                                    "status": "in_progress",
                                    "title": label_a[: TITLE_BUDGET + 10],
                                    "content": []}}}))
                            counters["low_folded"] = counters.get("low_folded", 0) + 1
                            return
                        is_think = upd.get("kind") == "think"
                        cid_a = f"{KEY_PREFIX}{sid_a}:think"
                        think_cards[(sid_a, tcid_a)] = cid_a
                        open_upd = {"sessionUpdate": "tool_call",
                                    "toolCallId": cid_a, "kind": "think",
                                    "status": "in_progress",
                                    "title": label_a[: TITLE_BUDGET + 10],
                                    "content": []}
                        if not is_think:
                            # announce a non-think tool's completion state on the
                            # row only when the tool reports it on its first line
                            if status_a == "completed":
                                open_upd["status"] = "completed"
                            elif status_a == "failed":
                                open_upd["status"] = "failed"
                        if sid_a not in think_open:
                            think_open.add(sid_a)
                        else:
                            # row exists already: Zed updates by id in place
                            open_upd["sessionUpdate"] = "tool_call_update"
                            open_upd.pop("kind", None)
                            if status_a in ("completed", "failed"):
                                open_upd["status"] = status_a
                        counters["low_folded"] = counters.get("low_folded", 0) + 1
                        send(dst, dumps_line({"jsonrpc": "2.0",
                                              "method": "session/update",
                                              "params": {"sessionId": sid_a,
                                                        "update": open_upd}}))
                        return
                if TITLES_ON and isinstance(upd, dict):
                    cap_tool_output(upd)
                    new = rewrite_tool_line(msg, tool_titles)
                    if new:
                        out = new
                if (
                    m == "breadcrumbs"
                    and isinstance(upd, dict)
                    and upd.get("sessionUpdate") in ("tool_call", "tool_call_update")
                    and isinstance(sid_t, str)
                    and upd.get("status") in ("completed", "failed")
                    and isinstance(upd.get("toolCallId"), str)
                ):
                    done = seen_tools.setdefault(sid_t, set())
                    tcid = upd["toolCallId"]
                    label = upd.get("title")
                    if tcid not in done and isinstance(label, str) and label.strip():
                        done.add(tcid)
                        lines = cards.setdefault(sid_t, [])
                        text = ("✗ " if upd["status"] == "failed" else "") + label.strip()
                        if not lines or lines[-1] != text:
                            lines.append(text)
                            del lines[:-CARD_LINES]
        if m == "breadcrumbs" and pending:
            flush()
        send(dst, out)

    def flush() -> None:
        for sid, pieces in pending.items():
            text = digest("".join(pieces))
            if text:
                lines = cards.setdefault(sid, [])
                if not lines or lines[-1] != text:
                    lines.append(text)
                    del lines[:-CARD_LINES]
                body = "\n".join(f"• {line}" for line in lines)
                if len(body) > CARD_BUDGET:
                    body = body[-CARD_BUDGET:]
                if body != last_sent.get(sid):
                    if os.environ.get("ZED_RELAY_APPEND") == "1":
                        send(dst, synth_thought(sid, text))  # legacy per-segment append
                    elif keyed:
                        send(dst, synth_keyed_thought(sid, card_id(sid), body))
                    else:
                        cid = opened.setdefault(sid, card_id(sid))
                        title = f"Thinking · {text}"
                        send(dst, synth_activity(sid, cid, title[:TITLE_BUDGET + 10], body,
                                                 sid not in sent_once))
                        sent_once.add(sid)
                        steps[sid] = steps.get(sid, 0) + 1
                    last_sent[sid] = body
                    counters["breadcrumbs"] += 1
        pending.clear()

    ticks = 0
    for line in src:
        m = mode.get() if mode is not None else "detailed"
        ticks += 1
        if ticks % LOG_EVERY == 0:
            log_counters(counters, m, "live")
        if STOP_TOKEN in line:
            # The prompt response marks the turn's end: close the live activity card.
            try:
                msg = json.loads(line)
            except ValueError:
                msg = None
            counters["passed"] += 1
            if isinstance(msg, dict) and isinstance(msg.get("result"), dict) \
                    and "stopReason" in msg["result"]:
                if m == "breadcrumbs" and pending:
                    flush()
                sid = state["prompts"].pop(msg.get("id"), None) if msg.get("id") is not None else None
                close_card(sid)  # close the live card before the turn-end response
                if sid in think_open:  # off mode: finish the static activity row
                    think_open.discard(sid)
                    send(dst, dumps_line({
                        "jsonrpc": "2.0", "method": "session/update",
                        "params": {"sessionId": sid, "update": {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": f"{KEY_PREFIX}{sid}:think",
                            "status": "completed", "title": "Done",
                            "content": []}}}))
            send(dst, line)
            continue
        reset = state["cards_reset"]
        if reset:
            for sid in reset:
                cards.pop(sid, None)
                last_sent.pop(sid, None)
                seen_tools.pop(sid, None)
                sent_once.discard(sid)
                think_open.discard(sid)
                for k in [k for k in think_cards if k[0] == sid]:
                    think_cards.pop(k, None)
            del reset[:]
        stripped = line.strip()
        if m == "off" and TOOL_ID_B in stripped and THOUGHT_TOKEN not in stripped:
            # Cheap gate for the static thinking fold: fully parsed below.
            counters["passed"] += 1
            emit(line, stripped, None, m)
            continue
        if option_enabled():
            if SESSION_ID_B in stripped and RESULT_B in stripped:
                line = add_option(line, m, state)
            if CONFIG_UPDATE_B in stripped:
                line = merge_option_update(line, m, state)
        if CHILD_METHOD_B in stripped or CHILD_META_B in stripped:
            try:
                msg = json.loads(stripped)
            except ValueError:
                msg = None
            if isinstance(msg, dict) and is_child_update(msg):
                forward = handle_child(msg, m, state, counters)
                if forward is None:
                    if m == "breadcrumbs" and pending:
                        flush()
                    counters["child_dropped"] += 1
                    continue
                counters["passed"] += 1
                send(dst, forward)
                continue
        if m == "detailed":
            counters["passed"] += 1
            emit(line, stripped, None, m)  # thoughts byte-for-byte; tool titles shortened
            continue
        if THOUGHT_TOKEN not in stripped:
            counters["passed"] += 1
            emit(line, stripped, None, m)
            continue
        try:
            msg = json.loads(stripped)
        except ValueError:
            counters["passed"] += 1
            emit(line, stripped, None, m)
            continue
        thought = thought_update(msg) if isinstance(msg, dict) else None
        if thought is None:
            counters["passed"] += 1
            emit(line, stripped, msg, m)
            continue
        sid, text = thought
        if m == "off":
            counters["dropped"] += 1
            continue
        # breadcrumbs: buffer; flush at segment end.
        pieces = pending.setdefault(sid, [])
        if pending_len < SEGMENT_CAP:
            pieces.append(text)
            pending_len += len(text)
        counters["buffered"] += 1
    if pending:
        flush()
    for sid in list(opened):
        close_card(sid)


def watch_mode(mode: ModeFile, state: dict, dst, stop: threading.Event) -> None:
    """Announce mode changes to Zed's picker even when the agent is idle.

    pump_out reads the mode file on the next child line, so a flip from
    `toggle-thinking.sh` (or a hand-edit) during a quiet moment would leave the
    composer showing a stale value. One notification per change, for every
    session whose option list we have seen."""
    while not stop.wait(0.5):
        try:
            m = mode.get()
            with MODE_LOCK:
                if m == state.get("announced"):
                    continue
                state["announced"] = m
            if not option_enabled():
                continue
            for sid in set(state["turns"]) | set(state["options"]):
                send(dst, synth_config_update(sid, m, state))
        except (BrokenPipeError, ValueError, OSError):
            return


def pump_raw(src, dst, counters: dict, state: dict, mode: ModeFile,
             zed_out) -> None:
    """Zed -> child: never filtered, except a pick of the relay's own Thinking
    option, which is answered here and never reaches the agent. On stdin EOF
    (Zed closed the pipe), close
    the child's stdin so a well-behaved agent exits and the outbound pump ends.
    Counts session/prompt requests per session to give each turn its own keyed
    thought card id."""
    PROMPT_TOKEN = b'"session/prompt"'
    for line in src:
        counters["passed"] += 1
        if PROMPT_TOKEN in line:
            try:
                msg = json.loads(line)
                sid = (msg.get("params") or {}).get("sessionId")
                if isinstance(sid, str):
                    state["turns"][sid] = state["turns"].get(sid, 0) + 1
                    state["cards_reset"].append(sid)
                    if msg.get("id") is not None:
                        state["prompts"][msg["id"]] = sid
            except ValueError:
                pass
        answer = option_answer(line, mode, counters, state) if SET_CONFIG_B in line else None
        if answer is not None:
            counters["passed"] += 1
            send(zed_out, answer)
            continue
        dst.write(line)
        dst.flush()
    try:
        dst.close()
    except OSError:
        pass


def main() -> int:
    forced, mode_file, child_cmd = parse_args(sys.argv[1:])
    mode = ModeFile(mode_file, forced or DEFAULT_MODE)
    if forced:
        mode._mode = forced  # --keep is authoritative while set

    try:
        child = subprocess.Popen(
            child_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            bufsize=0,
        )
    except OSError as exc:
        sys.stderr.write(f"acp-thought-filter: cannot start {child_cmd}: {exc}\n")
        return 1

    counters = {"passed": 0, "dropped": 0, "buffered": 0, "breadcrumbs": 0, "child_dropped": 0,
                "child_forwarded": 0, "option_sets": 0, "low_folded": 0}
    state = {"turns": {}, "cards_reset": [], "prompts": {}, "child_parent": {},
             "options": {}, "announced": mode.get()}
    # A 'start' line names the child immediately, so a running instance is identifiable
    # as a real Zed launch before its first 'exit' summary exists.
    log_counters(counters, mode.get(), "start", child_cmd[0])

    inbound = threading.Thread(
        target=pump_raw, args=(sys.stdin.buffer, child.stdin, counters, state,
                               mode, sys.stdout.buffer), daemon=True
    )
    inbound.start()

    stop_watch = threading.Event()
    threading.Thread(target=watch_mode,
                     args=(mode, state, sys.stdout.buffer, stop_watch),
                     daemon=True).start()

    try:
        pump_out(child.stdout, sys.stdout.buffer, mode, counters, state)
    except (BrokenPipeError, ValueError, OSError):
        pass
    finally:
        stop_watch.set()
        try:
            child.stdin.close()
        except OSError:
            pass
        child.wait()

    try:
        log_counters(counters, mode.get(), "exit", child_cmd[0])
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
