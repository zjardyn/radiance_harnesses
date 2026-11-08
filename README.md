# radiance_harnesses

Portable agent-harness configuration for the Radiance stack: OpenCode as the agent
server, Magic Context as the context plugin, llama-swap (`radiance`) as the provider,
and Zed as the editor client. One repo, identical behaviour on every machine.

## Layout

| repo file | installs to | what it controls |
| --- | --- | --- |
| `opencode/opencode.json` | `~/.config/opencode/opencode.json` | providers, model IDs, context/output limits, variants, default model, `default_agent`, plugins |
| `opencode/agent/me.md` | `~/.config/opencode/agent/me.md` | the system prompt (frontmatter = agent config, body = prompt), registered as `default_agent` |
| `magic-context/magic-context.jsonc` | `~/.config/cortexkit/magic-context.jsonc` | Magic Context: summarizer models, `prompt_surface` per model |
| `zed/settings-macos.json` | `~/Library/Application Support/Zed/settings.json` | Zed app settings: `agent_servers."OpenCode Local"` (command, env, default model/effort), `language_models.openai_compatible.radiance` |
| `zed/server-settings.json` | `~/.config/zed/settings.json` (remote hosts) | Zed *server* settings for SSH workspaces: `ssh_connections`, agent tool permissions |
| `shell/zshenv` | `~/.zshenv` | `OPENCODE_DISABLE_FREE_MODELS=1` for every terminal-launched client |
| `secrets.env.example` | source from your shell profile | `RADIANCE_API_KEY`, never committed |

## Install

```sh
git clone <your-remote> ~/radiance_harnesses && cd ~/radiance_harnesses
./install.sh              # symlinks each file into place, backing up what exists
```

`install.sh` is idempotent: existing real files move to `<name>.pre-radiance`, existing
symlinks are replaced. It skips Zed app settings on Linux (that path is Zed's server
settings there) and appends the env export to `~/.zshenv` rather than replacing it.

## Provider

`radiance` is a llama-swap endpoint; model IDs are **declared by hand** because
`/v1/models` is API-key gated and OpenCode cannot auto-discover them.

| model | context | output | variants |
| --- | --- | --- | --- |
| `swift15-flash-next` | 409,600 | 16,384 | `low` / `medium` / `xhigh` (`reasoning_effort`) |
| `gemma4-26b` | 131,072 | 16,384 | — |
| `qwen35` | 131,072 | 16,384 | — |
| `qwen-image-2.1-turbo` | image model — served by llama-swap, not registered in OpenCode | | |

Set the endpoint per machine if it differs: the base URL lives in
`opencode/opencode.json` → `provider.radiance.options.baseURL`.

## Secrets and state — never commit

- `~/.config/opencode/service.json` — plaintext basic-auth password for the local server.
- `~/.local/share/opencode/` — `opencode.db`, logs, session storage.
- `~/.local/share/cortexkit/magic-context/` — `context.db` (memories, notes, archive) and the
  embedding `models/` directory. This is per-machine history; sharing it leaks project data.
- `~/.cache/opencode/npm/` — installed plugin copies.
- Any real `RADIANCE_API_KEY`.

`.gitignore` covers these.

## Verifying after install

```sh
opencode models                                   # radiance models listed, no opencode/ free models
opencode api get /api/agent | grep -c '"id":"me"' # agent registered
jq . ~/.config/opencode/opencode.json >/dev/null && echo valid
```

Free-model hiding is env-only (`OPENCODE_DISABLE_FREE_MODELS=1`), read at server start;
there is no config field. Each launch path needs its own copy: `~/.zshenv` for terminals,
`agent_servers."OpenCode Local".env` for Zed, and `opencode service set env` for the shared
background service.

## Changing the system prompt

Edit `opencode/agent/me.md`; the body is the prompt. Reload with `/reload` in the TUI,
or restart the client. Magic Context injects its own tool contract on top of the agent,
so `me.md` governs persona, style, environment facts and project rules — not the plumbing.
