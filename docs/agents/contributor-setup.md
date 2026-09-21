# contributor-setup.md — one-time machine setup

What a contributor sets up once, before claiming anything. The work itself
is in `CONTRIBUTOR.md`.

`scripts/join.sh` covers the rest — prover toolchain, `gh` auth, the Choir
install — and reports what it could not fix. It also puts `choir` on your
PATH, which every command in `CONTRIBUTOR.md` assumes; if it flags that
`~/.local/bin` is not on yours, fix that before claiming anything.

## Mathlib search tooling

For an LLM proving in Lean, premise selection is the highest-leverage tool
there is: most proof failures are "couldn't find, or hallucinated, the
Mathlib lemma." Choir recommends your agent be able to search Mathlib, and
reminds you at claim time if you have not declared a tool. It is advisory,
never blocking, and Choir never inspects your agent.

Declare what you have in `~/.choir/config.json`:

```json
{ "tooling": { "search": "lean-lsp-mcp" } }
```

A tool name, `"agent-provided"` (your agent searches internally), or
`"none"` (deliberately going without) all silence the reminder. Leaving it
unset is what triggers the nudge.

The tools — LeanSearch (semantic), Loogle (pattern/type), Lean Finder
(intent-tuned) — are not binaries; they reach your agent through an MCP
server:

- **Claude Code**: `claude mcp add lean-lsp uvx lean-lsp-mcp` (add `-s project`
  to scope it to the repo), then set `"search": "lean-lsp-mcp"`. Or install
  [`cameronfreer/lean4-skills`](https://github.com/cameronfreer/lean4-skills)
  for the fuller pack.
- **Cursor / VSCode / Mistral Vibe**: register `lean-lsp-mcp` per its
  [README](https://github.com/oOo0oOo/lean-lsp-mcp).
- **Codex**: `codex mcp add lean-lsp -- uvx lean-lsp-mcp` (note the `--`
  before the command), then set `"search": "lean-lsp-mcp"`.
- **Gemini CLI**: via
  [`lean4-skills`](https://github.com/cameronfreer/lean4-skills), which is
  host-agnostic.

That config is machine-global and applies to every project you contribute
to. A `~/.choir/projects/<owner>/<repo>/config.json` overrides it for one
project; anything it omits is inherited.
