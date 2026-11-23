# Standalone `inaproc` CLI with AI mode — design (short)

Date: 2026-11-23 · Owner asked: "buatkan CLI komprehensif dengan mode AI pakai API key, setup command untuk input key,
desain bagus + logo ASCII, mudah dipakai, optimistic UI (proses di belakang terlihat), dan panduan jelas untuk Safari".

## Intent
Use every inaproc ability from a terminal without Claude Code / Claude Desktop: one `inaproc` command with a
friendly UI and an AI assistant that runs on the user's own API key (Anthropic, OpenAI, OpenRouter, DeepSeek).

## Goals
- `inaproc` command installed on PATH by `install.sh`; everything still standard-library Python 3.8+.
- Home screen with ASCII logo, status (version, AI provider, penyedia login hint) and a command menu.
- `setup`: interactive wizard — pick provider, paste key (hidden input), pick model (listed live from the
  provider's models endpoint), test the key, save. Keys in macOS Keychain (`security`), else `~/.inaproc/keys.json`
  (0600); env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`) override.
- `whoami`, `login`, `doctor` (checks Python, network, Safari settings, Automation permission, penyedia login,
  AI key) with step-by-step Indonesian instructions and offers to open the right Safari / System Settings page.
- `ai` (chat REPL) and `ask "…"` (one shot): the model drives the existing CLI through tools.
- Optimistic UI: a live step list on stderr shows each background action as it starts and finishes
  (thinking, each catalogue/penyedia call, category batches), with elapsed time; plain output when not a TTY or
  `NO_COLOR` is set.
- All existing commands work unchanged through `inaproc <command>`.

## Non-goals
- Streaming token-by-token output; Windows-native colour niceties beyond ANSI; storing keys in the repo.

## Anti-goals
- The AI sends a product change without the user confirming **in the terminal** (the CLI enforces it, not the model).
- API keys printed, logged, sent anywhere but the chosen provider, or written world-readable.

## Approach (decided)
Pure stdlib, raw HTTP to each provider (the project has a zero-dependency rule, so no SDKs). Two adapters:
Anthropic Messages API (`anthropic-version: 2026-10-21`, default model `claude-opus-5-5`, refusal fallbacks
`fallbacks: "default"` with beta `server-side-fallback-2026-07-01`, full assistant content echoed back each turn)
and OpenAI-compatible chat completions with function calling (OpenAI, OpenRouter `https://openrouter.ai/api/v1`,
DeepSeek `https://api.deepseek.com`). Alternatives considered: wrapping Claude Code (needs Claude Code — defeats
the purpose); one OpenAI-compatible shim for all (rejected: Claude must use its own API).

Tools given to the model: `inaproc(args)` runs the CLI in a subprocess (blocked: `penyedia apply`, `update`,
`discover`, `record`, `login`, `logout`), and `apply_plan(plan)` which shows the plan and asks the user y/N in
the terminal before running `penyedia apply`. System prompt = the skill's SKILL.md plus CLI notes.

## Left to the builder
Module layout, exact colours/glyphs, table widths, REPL slash commands.
