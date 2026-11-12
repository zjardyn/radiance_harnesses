#!/bin/zsh
# Run this ON THE LAPTOP to point OpenCode/Zed at Swift via llama-swap.
# Requires RADIANCE_API_KEY in the environment: source ~/radiance_harnesses/secrets.env
#   curl -fsSL http://10.0.0.168:8766/laptop-swift-setup.sh | zsh
set -euo pipefail

KEY="${RADIANCE_API_KEY:?RADIANCE_API_KEY is not set - source ~/radiance_harnesses/secrets.env}"
BASE='http://10.0.0.168:8000/v1'
ENV_FILE="$HOME/.config/radiance/secrets.env"

mkdir -p "$HOME/.config/radiance" "$HOME/.config/opencode"

cat >"$ENV_FILE" <<EOF
export RADIANCE_API_KEY='$KEY'
export RADIANCE_BASE_URL='$BASE'
export OPENCODE_DISABLE_FREE_MODELS=1
EOF
chmod 600 "$ENV_FILE"

# Ensure shell picks it up
for rc in "$HOME/.zshenv" "$HOME/.zshrc" "$HOME/.zprofile"; do
  touch "$rc"
  if ! grep -q 'radiance/secrets.env' "$rc" 2>/dev/null; then
    echo '' >>"$rc"
    echo "# Radiance llama-swap (Swift)" >>"$rc"
    echo "[[ -f \"$ENV_FILE\" ]] && source \"$ENV_FILE\"" >>"$rc"
  fi
done

# OpenCode provider config (create/patch if missing radiance block is heavy — write minimal overlay note)
if [[ -f "$HOME/.config/opencode/opencode.json" ]]; then
  python3 - <<'PY'
import json, os
from pathlib import Path
p = Path.home() / ".config/opencode/opencode.json"
d = json.loads(p.read_text())
prov = d.setdefault("provider", {}).setdefault("radiance", {})
opts = prov.setdefault("options", {})
opts["baseURL"] = "http://10.0.0.168:8000/v1"
opts["apiKey"] = "{env:RADIANCE_API_KEY}"
models = prov.setdefault("models", {})
models.setdefault("swift15-flash-next", {
    "name": "Swift 1.5 Flash-Next",
    "limit": {"context": 196608, "output": 16384},
})
d["model"] = "radiance/swift15-flash-next:xhigh"
p.write_text(json.dumps(d, indent=2) + "\n")
print("updated", p)
PY
else
  echo "No ~/.config/opencode/opencode.json — install radiance_harnesses first, or copy one from the server."
fi

# Live env for this shell
source "$ENV_FILE"

echo "=== checking server ==="
curl -fsS -m 5 -H "Authorization: Bearer $RADIANCE_API_KEY" http://10.0.0.168:8000/health
echo
curl -fsS -m 10 -H "Authorization: Bearer $RADIANCE_API_KEY" http://10.0.0.168:8000/v1/models \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); 
[print(" ", m["id"], "→", m.get("status",{}).get("value","?")) for m in d.get("data",[])]'

echo
echo "OK. Restart OpenCode / Zed so they pick up RADIANCE_API_KEY."
echo "Browser UI: open http://10.0.0.168:8000/ui/ and send header Authorization: Bearer \$RADIANCE_API_KEY"
echo "  or: open 'http://:${RADIANCE_API_KEY}@10.0.0.168:8000/ui/'"
