# radiance_harnesses

Agent-harness configuration for the Radiance stack: OpenCode as the agent server,
Magic Context as the context plugin, llama-swap (`radiance`) as the provider, Zed as the
editor client. One repo, identical behaviour on every machine. The live files are
symlinks into this repo, so `~/.config/...` **is** the tracked copy.

## Layout

| repo file | installs to | controls |
| --- | --- | --- |
| `opencode/opencode.json` | `~/.config/opencode/opencode.json` (box) | provider, model IDs, context/output limits, variants, default model, `default_agent`, `permissions`, plugins |
| `opencode/opencode.laptop.json` | `~/.config/opencode/opencode.json` (laptop) | same, for the machine that reaches the box over the LAN |
| `opencode/agent/radiance.md` | `~/.config/opencode/agent/radiance.md` | the system prompt: frontmatter = agent config, body = prompt |
| `magic-context/magic-context.jsonc` | `~/.config/cortexkit/magic-context.jsonc` | historian model, `prompt_surface` per model |
| `zed/settings-macos.json` | `~/Library/Application Support/Zed/settings.json` | Zed app settings: `agent_servers."OpenCode Local"` (command, env, effort), `language_models.openai_compatible.radiance` |
| `zed/server-settings.json` | `~/.config/zed/settings.json` (remote hosts) | Zed server settings for SSH workspaces: `ssh_connections`, agent tool permissions |
| `acp-thought-filter.py` | `~/.config/zed/acp-thought-filter.py` | ACP relay that publishes the Thinking picker on every machine |
| `shell/zshenv` | `~/.zshenv` | `OPENCODE_DISABLE_FREE_MODELS=1` for every terminal-launched client |
| `secrets.env.example` | source from your shell profile | `RADIANCE_API_KEY`, never committed |

## Install

```sh
git clone <your-remote> ~/radiance_harnesses && cd ~/radiance_harnesses
./install.sh              # symlinks each file into place, backing up what exists
```

Idempotent: existing real files move to `<name>.pre-radiance`, existing symlinks are
replaced. Zed app settings are skipped on Linux (that path is Zed's server settings
there); the env export is appended to `~/.zshenv`, not replaced.

## Serving spec

One llama-swap endpoint holds Swift on both cards. Nothing else is served.

| endpoint | role |
| --- | --- |
| `:8000` | llama-swap; Swift is resident |

A second model on the GPU evicts Swift mid-session, so a provider, profile, agent or
plugin that points at a non-resident model must not be declared — it can only fail, and
it puts a dead entry in the model menu. Historian work runs on Swift itself at
`variant: low`: same weights, so it costs GPU time and no extra VRAM.

## OpenCode spec

| key | value | why |
| --- | --- | --- |
| `model` | `radiance/swift15-flash-next:xhigh` | the chat you type runs at full effort |
| `default_agent` | `radiance` | `agent/radiance.md` is the only agent |
| `compaction.auto` / `compaction.prune` | `false` | Magic Context owns compaction |
| `plugin` | `@cortexkit/opencode-magic-context@latest` | no code-intelligence plugin: its 17 tools spend a local model's budget that `read` / `grep` / `shell` already cover |
| `provider.radiance.options.apiKey` | `{env:RADIANCE_API_KEY}` | never a literal in the file |
| `permissions` | `shell` `*` allow, then `ls*` `find*` `pwd*` `cd*` deny | the agent walks the tree with `glob` / `grep` / `read` and runs commands with a `workdir` instead of `cd`; rules are ordered, last match wins, and the scanner checks every subcommand, so `cd /x && git status` is denied too |

Declare the **real served window** in `limit.context`. OpenCode sizes its compaction
trigger from that number, so an over-declared limit means the session reaches the
provider's ceiling un-compacted and the request is refused.

| model | context | output | variants |
| --- | --- | --- | --- |
| `swift15-flash-next` | 262,144 | 16,384 | `low` / `medium` / `xhigh` (`reasoning_effort`) |
| `gemma4-26b` | 65,536 | 16,384 | `low` / `medium` / `xhigh`; not resident — reference entry only |

`qwen35` and `qwen-image-2.1-turbo` are llama-swap capabilities, not agents. They stay
out of the box's provider block; the laptop keeps the `qwen35` entry and nothing registers
it as an agent, so no request can evict Swift.

## Magic Context spec

```jsonc
"historian": { "maxTokens": 2048,
               "opencode": { "model": "radiance/swift15-flash-next", "variant": "low" } }
```

Top-level `historian`, not a profile: a profile only applies when selected. No `embedding`
block — keyword recall works without a vector server. `allow_home_project: true` on the box,
where sessions start outside a repository. `prompt_surface.models` marks a light-prose model
so Magic Context compresses its turns harder.

## Secrets and state — never commit

- `~/.config/opencode/service.json` — plaintext basic-auth password for the local server.
- `~/.local/share/opencode/` — `opencode.db`, logs, session storage.
- `~/.local/share/cortexkit/magic-context/` — `context.db` (memories, notes, archive) and the
  embedding `models/` directory. Per-machine history; sharing it leaks project data.
- `~/.cache/opencode/npm/` — installed plugin copies.
- `secrets.env` — real `RADIANCE_API_KEY`. Gitignored; only `secrets.env.example` is tracked.
- `~/.config/zed/settings.json` (live laptop agent env) and `~/.config/zed/sync-hosts.jsonc`
  (per-remote server layer, untracked) — these carry the real `RADIANCE_API_KEY`. The tracked
  `zed/settings-*.json` carry none: `sync-merge.py` merges the hosts layer over the base, so a
  composed push gets the key from `sync-hosts.jsonc`, and the laptop's own live file keeps it.

**This repo is public.** Nothing machine-specific, no paths to personal data, no keys.

## Verifying

```sh
opencode models                                          # radiance models, no opencode/ free models
opencode api get /api/agent | grep -c '"id":"radiance"'   # agent registered
jq '.provider.radiance.models["swift15-flash-next"].limit.context' ~/.config/opencode/opencode.json  # 262144
ss -ltn | grep -c ':8000'                                 # provider up
```

Free-model hiding is env-only (`OPENCODE_DISABLE_FREE_MODELS=1`), read at server start.
Every launch path needs its own copy: `~/.zshenv` for terminals,
`agent_servers."OpenCode Local".env` for Zed, `opencode service set env` for the shared
background service.

## Changing the system prompt

Edit `opencode/agent/radiance.md`; the body is the prompt. Reload with `/reload` in the TUI
or restart the client. Magic Context injects its own tool contract on top of the agent, so
`radiance.md` governs persona, style, environment facts and project rules — not the plumbing.
Relay changes need a new agent session; a running relay keeps the code it loaded.
