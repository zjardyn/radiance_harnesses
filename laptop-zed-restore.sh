#!/bin/zsh
# Restore MacBook Zed to Remote-SSH → box (where Thinking picker lives).
# Requires RADIANCE_API_KEY in the environment: source ~/radiance_harnesses/secrets.env
# The earlier laptop-swift-fix2.sh wrongly pushed a LOCAL OpenCode agent and
# wiped the Thinking UI. Run this ON THE LAPTOP:
#   curl -fsSL http://10.0.0.168:8766/laptop-zed-restore.sh | zsh
set -euo pipefail

ZED="$HOME/Library/Application Support/Zed"
mkdir -p "$ZED" "$HOME/.ssh" "$HOME/.config/radiance"

KEY="${RADIANCE_API_KEY:?RADIANCE_API_KEY is not set - source ~/radiance_harnesses/secrets.env}"
cat >"$HOME/.config/radiance/secrets.env" <<EOF
export RADIANCE_API_KEY='$KEY'
export RADIANCE_BASE_URL='http://10.0.0.168:8000/v1'
export OPENCODE_DISABLE_FREE_MODELS=1
EOF
chmod 600 "$HOME/.config/radiance/secrets.env"

# SSH host alias for Zed remote
if ! grep -q '^Host box$' "$HOME/.ssh/config" 2>/dev/null; then
  cat >>"$HOME/.ssh/config" <<'EOF'

Host box
  HostName 10.0.0.168
  User zhood
  IdentityFile ~/.ssh/id_ed25519
  ServerAliveInterval 60
  ServerAliveCountMax 6
EOF
  echo "added Host box to ~/.ssh/config"
else
  echo "Host box already in ~/.ssh/config"
fi

# Backup current (possibly mangled) settings
if [[ -f "$ZED/settings.json" ]]; then
  cp "$ZED/settings.json" "$ZED/settings.json.bak-pre-restore-$(date +%s)"
fi

# UI-only settings — NO agent_servers (that lives on Ubuntu via remote server)
cat >"$ZED/settings.json" <<'EOF'
{
  "theme": { "mode": "system" },
  "buffer_font_size": 15,
  "ui_font_size": 16,
  "terminal": { "font_size": 13 },
  "base_keymap": "Cursor",
  "vim_mode": true
}
EOF

# Keymap for toggle-thinking chord
cat >"$ZED/keymap.json" <<'EOF'
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

echo
echo "Restored laptop Zed to remote-client mode."
echo
echo "Next steps IN ZED:"
echo "  1. Cmd-Q Zed fully"
echo "  2. Reopen Zed"
echo "  3. Command palette → 'remote: Connect' → pick 'box'"
echo "  4. Open folder /home/zhood (or your project under it)"
echo "  5. Open Agent panel — Thinking picker should be back"
echo
echo "Verify SSH first if connect fails:"
echo "  ssh box 'pgrep -af acp-thought-filter; curl -s http://127.0.0.1:8000/health'"
