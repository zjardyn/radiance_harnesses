#!/bin/zsh
# HARD FIX — local Mac Zed that actually replies + Thinking picker.
# Requires RADIANCE_API_KEY in the environment: source ~/radiance_harnesses/secrets.env
#   curl -fsSL http://10.0.0.168:8766/laptop-zed-local.sh | zsh
set -euo pipefail

BOX='10.0.0.168'
KEY="${RADIANCE_API_KEY:?RADIANCE_API_KEY is not set - source ~/radiance_harnesses/secrets.env}"
BASE="http://${BOX}:8000/v1"
FETCH="http://${BOX}:8766"

ZED_APP="$HOME/Library/Application Support/Zed"
ZED_CFG="$HOME/.config/zed"
OC_CFG="$HOME/.config/opencode"
RAD="$HOME/.config/radiance"

mkdir -p "$ZED_APP" "$ZED_CFG" "$OC_CFG" "$RAD" "$HOME/.cache"

# --- resolve absolute binaries (Dock Zed has a tiny PATH) --------------------
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
  [[ -n "$c" && -x "$c" ]] && PY="$c" && break
done
OC=""
for c in /opt/homebrew/bin/opencode /usr/local/bin/opencode "$HOME/.opencode/bin/opencode" "$(command -v opencode 2>/dev/null || true)"; do
  [[ -n "$c" && -x "$c" ]] && OC="$c" && break
done

if [[ -z "$PY" ]]; then
  echo "FATAL: no python3. brew install python && re-run."
  exit 1
fi
if [[ -z "$OC" ]]; then
  echo "FATAL: no opencode binary. Install opencode, then re-run."
  exit 1
fi
echo "python3:  $PY"
echo "opencode: $OC"

# --- secrets -----------------------------------------------------------------
cat >"$RAD/secrets.env" <<EOF
export RADIANCE_API_KEY='$KEY'
export RADIANCE_BASE_URL='$BASE'
export OPENCODE_DISABLE_FREE_MODELS=1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:\$PATH"
EOF
chmod 600 "$RAD/secrets.env"
for rc in "$HOME/.zshrc" "$HOME/.zshenv"; do
  touch "$rc"
  grep -q 'radiance/secrets.env' "$rc" 2>/dev/null || \
    printf '\n[[ -f "$HOME/.config/radiance/secrets.env" ]] && source "$HOME/.config/radiance/secrets.env"\n' >>"$rc"
done

# OpenCode service env (GUI)
"$OC" service set env RADIANCE_API_KEY "$KEY" 2>/dev/null || true
"$OC" service set env RADIANCE_BASE_URL "$BASE" 2>/dev/null || true
"$OC" service set env OPENCODE_DISABLE_FREE_MODELS 1 2>/dev/null || true
"$OC" service restart 2>/dev/null || true

# --- lean laptop opencode.json (NO magic-context / historian plugins) --------
# Those plugins hang on the laptop waiting for 127.0.0.1:8010.
if [[ -f "$OC_CFG/opencode.json" ]]; then
  cp "$OC_CFG/opencode.json" "$OC_CFG/opencode.json.bak-pre-local-$(date +%s)"
fi
curl -fsSL "$FETCH/opencode/opencode.laptop.json" -o "$OC_CFG/opencode.json"
echo "wrote lean $OC_CFG/opencode.json"

# --- Thinking filter + wrapper with ABSOLUTE paths ---------------------------
curl -fsSL "$FETCH/acp-thought-filter.py" -o "$ZED_CFG/acp-thought-filter.py"
chmod +x "$ZED_CFG/acp-thought-filter.py"
printf 'detailed\n' >"$ZED_CFG/thinking-mode"

# Wrapper: Dock-safe PATH + env + log. This is what Zed launches.
WRAPPER="$ZED_CFG/acp-opencode.sh"
cat >"$WRAPPER" <<EOF
#!/bin/zsh
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:\$PATH"
export RADIANCE_API_KEY='$KEY'
export RADIANCE_BASE_URL='$BASE'
export OPENCODE_DISABLE_FREE_MODELS=1
export ZED_THINKING_MODE_FILE="\$HOME/.config/zed/thinking-mode"
exec "$PY" "\$HOME/.config/zed/acp-thought-filter.py" -- "$OC" acp
EOF
chmod +x "$WRAPPER"

# Direct (no filter) fallback — guaranteed text replies
DIRECT="$ZED_CFG/opencode-acp.sh"
cat >"$DIRECT" <<EOF
#!/bin/zsh
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:\$PATH"
export RADIANCE_API_KEY='$KEY'
export RADIANCE_BASE_URL='$BASE'
export OPENCODE_DISABLE_FREE_MODELS=1
exec "$OC" acp
EOF
chmod +x "$DIRECT"

# --- Zed settings ------------------------------------------------------------
if [[ -f "$ZED_APP/settings.json" ]]; then
  cp "$ZED_APP/settings.json" "$ZED_APP/settings.json.bak-pre-local-$(date +%s)"
fi

"$PY" - <<PY
import json
from pathlib import Path

zed = Path.home() / "Library/Application Support/Zed/settings.json"
wrap = str(Path.home() / ".config/zed/acp-opencode.sh")
direct = str(Path.home() / ".config/zed/opencode-acp.sh")
key = "$KEY"
base = "$BASE"

if zed.exists():
    raw = zed.read_text()
    try:
        d = json.loads(raw)
    except Exception:
        d = json.loads("\n".join(
            ln for ln in raw.splitlines() if not ln.strip().startswith("//")
        ))
else:
    d = {"base_keymap": "Cursor", "vim_mode": True}

agent = d.setdefault("agent", {})
agent["thinking_display"] = "always_expanded"
agent["default_model"] = {
    "provider": "radiance",
    "model": "swift15-flash-next",
}

# Primary = Thinking relay. Fallback = bare OpenCode (if filter ever flakes).
d["agent_servers"] = {
    "opencode": {
        "type": "custom",
        "command": wrap,
        "args": [],
        "env": {
            "RADIANCE_API_KEY": key,
            "RADIANCE_BASE_URL": base,
            "OPENCODE_DISABLE_FREE_MODELS": "1",
        },
        "default_config_options": {
            "effort": "low",
            "model": "radiance/swift15-flash-next:low",
        },
        "favorite_config_option_values": {
            "model": ["radiance/swift15-flash-next"],
            "effort": ["low", "medium", "xhigh"],
        },
    },
    "opencode-direct": {
        "type": "custom",
        "command": direct,
        "args": [],
        "env": {
            "RADIANCE_API_KEY": key,
            "RADIANCE_BASE_URL": base,
            "OPENCODE_DISABLE_FREE_MODELS": "1",
        },
        "default_config_options": {
            "effort": "low",
            "model": "radiance/swift15-flash-next:low",
        },
    },
}

zed.parent.mkdir(parents=True, exist_ok=True)
zed.write_text(json.dumps(d, indent=2) + "\n")
print("wrote", zed)
PY

cat >"$ZED_APP/keymap.json" <<'EOF'
[
  {
    "context": "workspace",
    "bindings": { "ctrl+super+t": ["task::Spawn", { "task_name": "toggle-thinking" }] }
  },
  {
    "context": "agent_panel",
    "bindings": { "ctrl+super+t": ["task::Spawn", { "task_name": "toggle-thinking" }] }
  }
]
EOF

cat >"$ZED_CFG/toggle-thinking.sh" <<'EOF'
#!/bin/sh
set -eu
mode_f="$HOME/.config/zed/thinking-mode"
settings="$HOME/Library/Application Support/Zed/settings.json"
mode=$(cat "$mode_f" 2>/dev/null || echo detailed)
case "$mode" in
  breadcrumbs) next=detailed ;;
  detailed)    next=off ;;
  *)           next=breadcrumbs ;;
esac
echo "$next" > "$mode_f"
case "$next" in
  detailed) disp=always_expanded ;;
  *)        disp=always_collapsed ;;
esac
if [ -f "$settings" ] && grep -q '"thinking_display"' "$settings"; then
  tmp=$(mktemp "$settings.XXXXXX")
  sed -e "s/\"thinking_display\"[[:space:]]*:[[:space:]]*\"[a-z_]*\"/\"thinking_display\": \"$disp\"/" \
      "$settings" >"$tmp"
  mv "$tmp" "$settings"
fi
echo "thinking display: $next"
EOF
chmod +x "$ZED_CFG/toggle-thinking.sh"

cat >"$ZED_APP/tasks.json" <<EOF
[
  {
    "label": "toggle-thinking",
    "command": "$HOME/.config/zed/toggle-thinking.sh",
    "mode": "application",
    "use_new_terminal": false,
    "allow_concurrent_runs": false,
    "reveal": "never",
    "tags": []
  }
]
EOF

# --- prove the wire from THIS machine ----------------------------------------
echo
echo "=== API check ==="
if ! curl -fsS -m 10 -H "Authorization: Bearer $KEY" "$BASE/models" \
  | "$PY" -c 'import sys,json; d=json.load(sys.stdin); print("models:", ", ".join(m["id"] for m in d.get("data",[])))'; then
  echo "FATAL: laptop cannot reach $BASE — fix Wi‑Fi/VPN first"
  exit 1
fi

echo "=== chat smoke (must print READY) ==="
curl -fsS -m 90 \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"swift15-flash-next","messages":[{"role":"user","content":"Reply with exactly: READY"}],"max_tokens":64,"reasoning_effort":"low"}' \
  "$BASE/chat/completions" \
| "$PY" -c 'import sys,json; m=json.load(sys.stdin)["choices"][0]["message"]; print(m.get("content") or "(EMPTY)")'

echo
echo "DONE. Now:"
echo "  1. Cmd-Q Zed (fully quit)"
echo "  2. Reopen Zed on a LOCAL folder (not remote:Connect)"
echo "  3. Agent panel → agent 'opencode' (Thinking on)"
echo "  4. If that still blanks: switch agent to 'opencode-direct'"
echo "  5. New chat, ask anything — default effort is low so it answers fast"
