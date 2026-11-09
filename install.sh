#!/usr/bin/env bash
# Install (default) or refresh ( --pull ) the Radiance harness configs.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS="$(uname -s)"

link() { # link <repo-relative-src> <absolute-dst>
  local src="$REPO/$1" dst="$2"
  [ -f "$src" ] || { echo "skip  $1 (not in repo)"; return; }
  mkdir -p "$(dirname "$dst")"
  if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
    echo "ok    $dst"
  elif [ -e "$dst" ]; then
    mv "$dst" "$dst.pre-radiance"
    ln -s "$src" "$dst"
    echo "link  $dst  (previous file kept as $(basename "$dst").pre-radiance)"
  else
    ln -s "$src" "$dst"
    echo "link  $dst"
  fi
}

pull() { # pull <repo-relative-dst> <absolute-src>
  local dst="$REPO/$1" src="$2"
  [ -e "$src" ] || { echo "skip  $src (absent)"; return; }
  if [ -L "$src" ]; then
    echo "ok    $1 (live path is a symlink into this repo)"
    return
  fi
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  echo "pull  $src -> $1"
}

case "${1:-install}" in --*) set -- "${1#--}" ;; esac
case "${1:-install}" in
  install)
    link opencode/opencode.json             "$HOME/.config/opencode/opencode.json"
    link opencode/agent/me.md               "$HOME/.config/opencode/agent/me.md"
    link opencode/agent/radiance.md         "$HOME/.config/opencode/agent/radiance.md"
    link magic-context/magic-context.jsonc  "$HOME/.config/cortexkit/magic-context.jsonc"
    if [ "$OS" = "Darwin" ]; then
      link zed/settings-macos.json          "$HOME/Library/Application Support/Zed/settings.json"
    else
      echo "skip  Zed app settings on $OS (that file is Zed's server settings here)"
    fi
    if [ "${RADIANCE_SSH_HOST:-0}" = "1" ]; then
      link zed/server-settings.json         "$HOME/.config/zed/settings.json"
    fi
    # ~/.zshenv is shared shell config: append the export, never replace the file.
    touch "$HOME/.zshenv"
    if ! grep -q OPENCODE_DISABLE_FREE_MODELS "$HOME/.zshenv"; then
      printf '\n# radiance_harnesses\nexport OPENCODE_DISABLE_FREE_MODELS=1\n' >> "$HOME/.zshenv"
      echo "edit  ~/.zshenv (export appended)"
    else
      echo "ok    ~/.zshenv (export present)"
    fi
    echo
    echo "set RADIANCE_API_KEY in your environment (see secrets.env.example)"
    echo "for the shared background service also run:"
    echo "  opencode service set env OPENCODE_DISABLE_FREE_MODELS 1"
    echo "verify:  opencode models"
    ;;
  pull)
    pull opencode/opencode.json            "$HOME/.config/opencode/opencode.json"
    pull opencode/agent/me.md              "$HOME/.config/opencode/agent/me.md"
    pull opencode/agent/radiance.md        "$HOME/.config/opencode/agent/radiance.md"
    pull magic-context/magic-context.jsonc "$HOME/.config/cortexkit/magic-context.jsonc"
    if [ "$OS" = "Darwin" ]; then
      pull zed/settings-macos.json         "$HOME/Library/Application Support/Zed/settings.json"
    else
      pull zed/settings-linux.json         "$HOME/.config/zed/settings.json"
    fi
    pull shell/zshenv                      "$HOME/.zshenv"
    echo "review the diff before committing"
    ;;
  *)
    echo "usage: $0 [install|pull]" >&2
    exit 2
    ;;
esac
