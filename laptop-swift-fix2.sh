#!/bin/zsh
# Second-pass laptop fix: put RADIANCE_API_KEY where Zed/OpenCode GUI can see it.
# Requires RADIANCE_API_KEY in the environment: source ~/radiance_harnesses/secrets.env
#   curl -fsSL http://10.0.0.168:8766/laptop-swift-fix2.sh | zsh
set -euo pipefail

KEY="${RADIANCE_API_KEY:?RADIANCE_API_KEY is not set - source ~/radiance_harnesses/secrets.env}"
export KEY
BASE='http://10.0.0.168:8000/v1'
ENV_FILE="$HOME/.config/radiance/secrets.env"

mkdir -p "$HOME/.config/radiance"
cat >"$ENV_FILE" <<EOF
export RADIANCE_API_KEY='$KEY'
export RADIANCE_BASE_URL='$BASE'
export OPENCODE_DISABLE_FREE_MODELS=1
EOF
chmod 600 "$ENV_FILE"
source "$ENV_FILE"

# 1) OpenCode background service (GUI launches often use this, not your shell)
if command -v opencode >/dev/null 2>&1; then
  opencode service set env RADIANCE_API_KEY "$KEY" 2>/dev/null || true
  opencode service set env OPENCODE_DISABLE_FREE_MODELS 1 2>/dev/null || true
  opencode service set env RADIANCE_BASE_URL "$BASE" 2>/dev/null || true
  # restart service if present
  opencode service restart 2>/dev/null || true
  echo "opencode service env updated"
else
  echo "opencode not on PATH — skip service env"
fi

# 2) Patch Zed app settings so Dock-launched Zed gets the key
ZED_SETTINGS="$HOME/Library/Application Support/Zed/settings.json"
if [[ -f "$ZED_SETTINGS" ]]; then
  python3 - <<'PY'
import json, os
from pathlib import Path
p = Path.home() / "Library/Application Support/Zed/settings.json"
raw = p.read_text()
# strip // comments lightly for jsonc-ish files
lines = []
for line in raw.splitlines():
    s = line.strip()
    if s.startswith("//"):
        continue
    lines.append(line)
text = "\n".join(lines)
# try strict json first, then fallback
try:
    d = json.loads(raw)
except Exception:
    d = json.loads(text)
agents = d.setdefault("agent_servers", {})
# common keys
for name in list(agents.keys()):
    block = agents[name]
    if not isinstance(block, dict):
        continue
    env = block.setdefault("env", {})
    env["RADIANCE_API_KEY"] = os.environ["KEY"]
    env["OPENCODE_DISABLE_FREE_MODELS"] = "1"
    env["RADIANCE_BASE_URL"] = "http://10.0.0.168:8000/v1"
# also language model provider if present
lm = d.get("language_models", {}).get("openai_compatible", {}).get("radiance")
if isinstance(lm, dict):
    lm["api_url"] = "http://10.0.0.168:8000/v1"
    lm["api_key"] = os.environ["KEY"]
p.write_text(json.dumps(d, indent=2) + "\n")
print("patched", p)
PY
else
  echo "No Zed settings at $ZED_SETTINGS (ok if you only use OpenCode TUI)"
fi

# 3) Prove the wire works with a visible answer
echo
echo "=== direct chat test (should print READY) ==="
curl -fsS -m 60 \
  -H "Authorization: Bearer $RADIANCE_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"swift15-flash-next","messages":[{"role":"user","content":"Reply with exactly: READY"}],"max_tokens":64,"reasoning_effort":"low"}' \
  "$BASE/chat/completions" \
| python3 -c 'import sys,json; m=json.load(sys.stdin)["choices"][0]["message"]; print(m.get("content") or "(empty content)"); print("reasoning:", (m.get("reasoning_content") or "")[:80])'

echo
echo "=== models ==="
curl -fsS -m 10 -H "Authorization: Bearer $RADIANCE_API_KEY" "$BASE/models" \
| python3 -c 'import sys,json; 
[print(m["id"], m.get("status",{}).get("value")) for m in json.load(sys.stdin).get("data",[])]'

echo
echo "Now: fully quit Zed (Cmd-Q) and OpenCode, reopen, start a NEW agent chat."
echo "If the panel still looks empty, set Thinking to 'Detailed' or 'card' in the composer."
echo "Browser UI: open 'http://:${KEY}@10.0.0.168:8000/ui/'"
