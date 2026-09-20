# Dogfood MCP: growing capabilities pane to pane

Jcode reads MCP config at pane start, so an agent can't hot-load a new
tool into its own running session. The sustainable answer is a
convention, not a reload hack: agents **build** tools mid-session and
the **next pane** wields them.

## Convention

`~/dogfood-mcp/<name>/` holds one MCP stdio server:

```
~/dogfood-mcp/
  plume/            # e.g. HackMIT submission drafting
    server.py       # MCP stdio server (required)
    mcp.json        # optional: {"command", "args", "env"} override
  gh-search/
    server.py
```

Bare `server.py` runs as `<python> server.py`. `mcp.json` overrides for
non-python runtimes or extra env (tokens live in `env`, never in code).

## The loop

```bash
clique mcp grow        # validate + register everything into all harnesses
```

`grow` smoke-checks each server (compiles, mentions mcp, spawns sanely),
skips broken ones with a reason, and writes a named entry into every
harness config on the box (jcode, claude, codex, cursor, ...). `clique`
itself re-registers first. Idempotent: run it any time.

**Every new pane then starts with the accumulated capabilities.** The
pool compounds tools instead of rebuilding them.

## Starter prompt for a jcode-on-clique pane

```text
You are dogfooding the clique. Start with clique_self_assess and note
what is missing or slow.

Convention: if you need a capability you don't have (web fetch for a
specific API, a parser, a checker), BUILD it as an MCP stdio server in
~/dogfood-mcp/<name>/server.py, then run `clique mcp grow` so the NEXT
pane inherits it. Leave a one-line README in the dir saying what it
does and what goal needed it.

Rules for grown servers:
- stdio transport, small surface (1-5 tools), no credentials in code
  (read env, document which vars `mcp.json` must set).
- Validate with the smoke check: `clique mcp grow` must report it valid.
- Never break the `clique` entry; your servers are additive.

Your goal this pane: <GOAL>. Use grown servers from prior panes when
they fit; grow new ones when they don't. Stop when the goal's
acceptance check passes or you have a concrete blocker to report.
```

## Trust notes

Grown servers run with the pane's privileges by design (they're the
agent's own hands). The smoke check guards against accidents (syntax,
missing deps), not malice. Review `~/dogfood-mcp/*/` diffs the way
you'd review any teammate's PR before opening panes that load them.

## Docs-sync: pushing closed-loop results back to GitHub

The socket VCS is truth for live work; GitHub remains the durable
public record. When a dogfood pane finishes verified work (code or
docs), it syncs outward with this exact sequence. Infrastructure only:
no pane ever commits or pushes by itself.

1. **Draft in the workspace.** All edits happen in the live workspace
   (`clique_workspace_write/patch`), never directly in the checkout.
2. **Export the bundle.** `clique_workspace_export` (MCP/server tool),
   `clique workspace --export <id>` (CLI), or
   `GET /v1/workspaces/<id>/export?paths=a.md,b.md` (REST). The bundle
   carries files + git provenance (checkpoint shas).
3. **Apply on a credentialed box.** `sh scripts/docs-sync.sh
   /tmp/bundle.json --checkout ~/git/tcj` dry-runs; `--apply` writes.
   Path guard rejects `..` and absolute paths.
4. **Human review gate.** Commit + push + PR stay manual. The agent
   reports the workspace id, export seq, and what changed; a human
   reviews the diff before it lands on main.

Starter-prompt line for docs-sync panes: "When your work is verified,
export the workspace and report the bundle seq plus a file list. Do
not commit, push, or open PRs."
