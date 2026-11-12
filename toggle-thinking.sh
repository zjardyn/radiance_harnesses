#!/bin/sh
# Cycle Zed's thinking display: breadcrumbs -> card -> detailed -> off ->
# breadcrumbs. The mode file is read live by acp-thought-filter.py (applies to
# incoming messages). agent.thinking_display is synced for presentation: detailed
# shows blocks expanded, the one-row modes (breadcrumbs, card, off) stay collapsed
# so the row can be clicked open.
# settings.json is JSONC (comments, trailing commas), so the one key is
# rewritten with sed instead of parsing the file.
set -eu

mode_f="$HOME/.config/zed/thinking-mode"
settings="$HOME/.config/zed/settings.json"

mode=$(cat "$mode_f" 2>/dev/null || echo breadcrumbs)
case "$mode" in
  breadcrumbs) next=card ;;
  card)        next=detailed ;;
  detailed)    next=off ;;
  *)           next=breadcrumbs ;;
esac
echo "$next" > "$mode_f"

case "$next" in
  detailed) disp=always_expanded ;;
  *)        disp=always_collapsed ;;
esac

tmp=$(mktemp "$settings.XXXXXX")
trap 'rm -f "$tmp"' EXIT
if grep -q '"thinking_display"' "$settings"; then
  sed -e "s/\"thinking_display\"[[:space:]]*:[[:space:]]*\"[a-z_]*\"/\"thinking_display\": \"$disp\"/" \
      "$settings" >"$tmp"
  mv "$tmp" "$settings"
  trap - EXIT
else
  rm -f "$tmp"
  trap - EXIT
  echo "warning: no thinking_display key in settings.json; left untouched" >&2
fi

echo "thinking display: $next (blocks: $disp)"
