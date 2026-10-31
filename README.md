# metacog

**Verify anything your coding agent claims.**

Coding agents (Claude Code, Muse Code, Codex) run shell commands, edit code and touch infrastructure, and then *tell* you what happened. The painful failures are not crashes. They are confident reports that are not true: "all tests pass" after a test was skipped, "deployed" when the rollout errored, a file that was never written.

metacog sits next to the agent and does three things:

| | What it does | When |
|---|---|---|
| **Verify** | Compares the agent's final report with the real tool log. A *different model family* (Meta **Muse Spark**) judges every claim as `verified` / `false` / `unverifiable`, citing log entries. False reports are sent back to the agent with the evidence. | end of every turn |
| **Guard** | Blocks destructive commands and data exfiltration *before* they run: `rm -rf ~`, secrets piped to the network, uploads to unknown hosts after reading credentials, an agent editing its own oversight hooks. Risky-but-normal actions (force push, `terraform apply`, `kubectl delete`) ask you first. | before every tool call |
| **Optimize** | Flags waste: the same file read again and again, retry loops on the same failing command, huge tool outputs that flood the context. | end of every turn |

Plus an **egress map**: every host the agent's processes connect to, including traffic that never shows up as a tool call (telemetry, MCP servers, the model provider itself).

## Install

```bash
uv tool install git+https://github.com/song2h/metacog   # or: pipx install git+https://github.com/song2h/metacog
metacog init                                           # stores your Meta Model API key, checks setup
```

No dependencies beyond the Python standard library (3.10+). Get a Meta Model API key at [dev.meta.ai](https://dev.meta.ai).

## Use

Try it for one session, nothing installed into your agent config:

```bash
metacog wrap claude              # Claude Code with metacog attached
metacog wrap muse                # Muse Code
metacog wrap muse exec "fix the failing test"   # any agent arguments pass through
```

The dashboard opens at <http://127.0.0.1:8977>: live timeline, guard decisions, per-claim verdicts with evidence, a trust score per session, and the egress map. metacog options go before the agent name: `metacog wrap --no-egress --port 9000 claude`.

Attach it to every session:

```bash
metacog install claude           # as a Claude Code plugin
metacog install muse             # hooks in ~/.config/muse/settings.json
metacog install codex            # hooks in ~/.codex/hooks.json (see "Status")
metacog uninstall <agent>
```

In Claude Code you can also install the plugin directly: `/plugin marketplace add song2h/metacog` then `/plugin install metacog@metacog` (the `metacog` CLI must be on your PATH).

Other commands: `metacog dashboard`, `metacog sessions`, `metacog show <session>`, `metacog doctor`.

## How it works

```
 agent (Claude Code / Muse Code / Codex)
   │  hooks: UserPromptSubmit · PreToolUse · PostToolUse(+Failure) · Stop
   ▼
 metacog hook ──► ~/.metacog/sessions/<id>.jsonl   (the source of truth)
   │
   ├─ PreToolUse ─► guard: deny / ask / flag
   └─ Stop ───────► verify: deterministic checks ─► Muse Spark (json_schema) ─► verdict
                    false claim?  ─► block stop, send evidence back to the agent (max 2 per turn)

 metacog wrap ──► local CONNECT proxy (HTTPS_PROXY) ─► egress map (host:port only, TLS is never decrypted)
 metacog dashboard ──► reads the JSONL files
```

- All three agents share one hook shape, so there is one entry point (`metacog hook <agent>`) and the differences live in a few lines.
- `wrap` injects hooks for one run only: `claude --settings <tmpfile>` and `muse -c hooks=<json>`. Your config files are not touched.
- **Deterministic checks first, model second.** Facts about *actions* are checked in code: a skip marker written into a test file, a test file deleted, an exit code masked with `|| true`, a file the report mentions that never existed. Whether the report *claims* something is a reading question (quotes, negation, partial success), so the model decides it. Without an API key, metacog still guards and records, and claim checks only warn.
- **Why a different model family:** the worker (e.g. Claude) and the checker (Muse Spark) do not share the same blind spots. The verifier sees only the final message and the tool log, never the worker's reasoning.

## Benchmark

`bench/run.py` measures the verifier on 20 cases: 10 false reports (failing tests reported as passing, skip-to-green, deleted test, never-written file, errored deploy, phantom doc, failed install, masked exit code, weakened assertion, failed build) and 10 honest reports, 4 of which are real Claude Code and Muse Code sessions recorded with metacog. Honest cases include partial failures and a report that quotes the phrase it refuses to say.

Results with `muse-spark-1.3`, each case run 3 times (2026-10-31):

| | Result |
|---|---|
| False reports caught | **29 / 30** (one miss: `phantom-doc`, 1 of 3 runs) |
| Honest reports flagged (false positives) | **0 / 30** |
| Latency per verification | median 4.3 s, max 9.5 s |
| Cost per verification | median $0.0043 |

"Detected" means metacog would send the report back. Cost uses Muse Spark 1.3 standard pricing ($1.25 / $4.25 per 1M input / output tokens, dev.meta.ai, 2026-10-31). The cases were written by the author, so treat this as a regression suite, not a general accuracy claim.

```bash
uv run python bench/run.py --repeat 3
```

## What leaves your machine

Only at the end of a turn, and only with an API key: the agent's final message and a clipped tool log (commands, outputs, written content) go to `api.meta.ai` for verification. metacog uses the standard `muse-spark-1.3` model, not the `-contributor` variant, because contributor-tier requests may be used for training. Session logs stay in `~/.metacog`. The dashboard binds to 127.0.0.1.

## Status and limits

| | Status |
|---|---|
| Claude Code 2.1 | tested end to end: record, deny, stop-and-retry, egress. `ask` decisions follow the documented hook contract but were not exercised yet |
| Muse Code 1.4 | tested end to end. Muse's hook format is not documented; metacog uses the Claude-compatible shape, verified by running it |
| Codex | hooks written from the Codex docs, **not tested yet** |

- Hooks see the agent's *tool calls*. They do not see what the agent binary itself sends; the egress map covers that only for programs that honor `HTTPS_PROXY`.
- The egress proxy also applies to commands the agent runs, so a request to an unresolvable host fails with a proxy `502` instead of a DNS error.
- The guard is pattern-based. It raises the cost of mistakes and obvious exfiltration; it is not a sandbox. Codex documents its own hooks the same way.
- Verification adds about 2–10 s at the end of a turn.

## Built during the Meta Global AI Developer Hackathon

Everything in this repository was written during the hackathon (Oct 26 – Nov 4, 2026); the commit history is the record. External pieces: Claude Code, Muse Code and Codex are the agents being supervised, and Meta Muse Spark (via the Meta Model API) is the verifier model.

## License

Apache-2.0
