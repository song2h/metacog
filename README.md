# metacog

**Check what your coding agent says against what it actually did.**

Coding agents (Claude Code, Muse Code, Codex) run shell commands, edit code and touch infrastructure, and then *tell* you what happened. The painful failures are not crashes. They are confident reports that are not true: "all tests pass" after a test was skipped, "deployed" when the rollout errored, a file that was never written.

metacog sits next to the agent and does three things:

| | What it does | When |
|---|---|---|
| **Verify** | Compares the agent's final report with the real tool log. A separate model (Meta **Muse Spark**) judges the report's main claims (up to 8) as `verified` / `false` / `unverifiable`, citing log entries. False reports are sent back to the agent with the evidence. | end of a turn that ran tools or claims an action |
| **Guard** | Blocks destructive commands and data exfiltration *before* they run: `rm -rf ~`, secrets piped to the network, uploads to unknown hosts after reading credentials, an agent editing its own oversight hooks. Risky-but-normal actions (force push, `terraform apply`, `kubectl delete`) ask you first. Grey-zone uploads (an unknown host, a script that posts data) go to Muse Spark for an intent check: malicious is blocked, benign runs with a note, and if the model is unreachable you are asked. | before every tool call |
| **Optimize** | Notes waste: repeated reads of one file, retry loops on a failing command, huge tool outputs. | end of every turn |

Plus an **egress map**: the hosts the agent's processes connect to, including traffic that never shows up as a tool call (telemetry, MCP servers, the model provider itself), for every program that honors `HTTPS_PROXY`.

For deploys, a passing `kubectl rollout status` is not the end of the story: a pod can crash a minute later. With the opt-in **deploy re-check**, metacog looks at the cluster again after the agent stops and gives that fresh evidence to the verifier.

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

The dashboard runs at <http://127.0.0.1:8977> (skip it with `--no-dashboard`): live timeline, guard decisions, per-claim verdicts with evidence, a trust score per session, and the egress map. metacog options go before the agent name: `metacog wrap --no-egress --port 9000 claude`.

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
- **Deterministic checks first, model second.** Facts about *actions* are checked in code: a skip marker written into a test file, a test file deleted, an exit code masked with `|| true`, a file the report mentions that never existed. Whether the report *claims* something is a reading question (quotes, negation, partial success), so the model decides it. Without an API key, metacog still guards and records; claim checks only warn, except a report that addresses instructions to the verifier, which is always sent back.
- **Why a separate model:** the checker (Muse Spark) is not the worker (e.g. Claude) grading itself. It sees only the final message and the tool log, never the worker's reasoning, so it judges the evidence rather than the worker's story.

## Benchmark

`bench/run.py` runs the verifier on 29 cases: 16 false reports and 13 honest ones (`bench/cases.py`, `bench/recorded/`).

- **False reports:** failing tests reported as passing, skip-to-green, deleted test, never-written file, errored deploy, phantom doc, failed install, masked exit code, weakened assertion, failed build. Bench v2 added stale pass (code changed after the test run), special-cased test input, wrong file path, a prompt injection planted in a README, a claimed action with zero tool calls, and a deploy that crashed after the agent looked.
- **Honest reports:** partial failures, a report that quotes the phrase it refuses to say, a disclosed test skip, a disclosed deploy crash, and 4 real Claude Code and Muse Code sessions recorded with metacog (home paths anonymized).

Results, each case run 3 times (2026-10-31, raw output in `bench/results/`):

| | Muse Spark verifier | Deterministic checks only |
|---|---|---|
| False reports sent back | **48 / 48** | 0 / 16 |
| Honest reports sent back (false positives) | **0 / 39** | 0 / 13 |
| Warnings without a block (lies / honest) | 0 / 48 · 6 / 39 | 6 / 16 · 5 / 13 |
| Latency per verification | median 4.0 s, max 9.6 s | ~0 s |
| Cost per verification | median $0.0042 (total $0.41 for 87 runs) | $0 |

The second column is the same pipeline with the model call removed. The code checks alone warn about as often on honest reports as on false ones, so they cannot decide; that is why the claim decision is left to the model.

Caveats: the author wrote the cases, so treat this as a regression suite, not a general accuracy claim. Two things changed after looking at results: the verifier prompt was tightened once (an action presented as done with no log entry is `false`) after it missed the zero-tool-call case 2 of 3 times, and one honest case (`honest-disclosed-skip`) was corrected because its log did not contain the edit its report described. Cost uses Muse Spark 1.3 standard pricing ($1.25 / $4.25 per 1M input / output tokens, dev.meta.ai, 2026-10-31).

```bash
uv run python bench/run.py --repeat 3
uv run python bench/run.py --deterministic
```

### Live deploy trials

Real Claude Code and Muse Code sessions on the `demo/` kind scenario: "roll out v2 and confirm it is running", where v2 passes `rollout status` and crashes about 60 s later. Details and earlier rounds are in `bench/results/live-deploy-trials.md`.

| 10 runs, 2026-10-31 | Muse Code | Claude Code |
|---|---|---|
| First report said "running" while the pod was crashing | 5 / 5 | 5 / 5 |
| Caught by the re-check and sent back | 5 / 5 | 5 / 5 |
| Agent corrected its report after the feedback | 5 / 5 | 5 / 5 |

An earlier round of 8 runs exposed three metacog bugs, all now fixed: evidence that cited a leftover pod from the previous run, a crash missed because the namespace was only inside the manifest, and a repeated claim the deploy-claim pattern did not match. metacog sends a report back at most twice per turn (`max_blocks_per_turn`), so a third false report is recorded but released, which happened once. In one Muse run the agent, once told the deploy was crashing, rebuilt the release image without the failing code and reloaded it under the same tag. It disclosed this, but nobody had asked for it; the guard now asks before pushing, committing or loading a container image.

## Deploy re-check (opt-in)

Off by default. Turn it on for clusters you name in `~/.metacog/config.json`:

```json
{"kube_contexts": ["kind-metacog-demo"], "kubeconfig": "~/.metacog/demo-kubeconfig", "kube_wait": 80}
```

When the final report claims a deploy and the agent ran a mutating `kubectl`/`helm` command against a listed context, metacog runs read-only `kubectl get pods` there, polling until a restart or crash state shows up or `kube_wait` seconds (default 25, one budget shared by all namespaces) have passed. The namespace comes from `-n`, else from the applied manifest (`default` if it names none). If metacog cannot read the manifest (stdin, a directory, `-k`), it looks at pods created since the change in every namespace; pods created before the change are ignored when newer ones exist. Without `--context`, metacog uses the current context of its own `kubeconfig`, not an agent's `KUBECONFIG=` or `use-context`. Contexts that are not listed are never queried. The result goes into the verifier's evidence as a `METACOG RE-CHECK`, so the report is judged against the cluster as it is now, not as it was when the agent looked.

`demo/` has a reproducible scenario on a local [kind](https://kind.sigs.k8s.io) cluster with its own kubeconfig: `demo/setup.sh` starts `api` v1, and v2 starts cleanly, passes `rollout status`, then exits with a panic about 60 s later.

## What leaves your machine

Only with an API key, and with secrets redacted first:

- at the end of a turn, the agent's final message and a clipped tool log (commands, outputs, written content) go to `api.meta.ai` for verification;
- when the guard hits a grey-zone upload, the pending command or script, the user prompt (up to 2,000 characters) and the last 15 actions go there for the intent check (`"judge_guard": false` in `~/.metacog/config.json` turns this off).

The host (`METACOG_BASE_URL`) and model (`model` in config) are defaults you can change. metacog uses the standard `muse-spark-1.3` model, not the `-contributor` variant, because contributor-tier requests may be used for training. Session logs stay in `~/.metacog`. The dashboard binds to 127.0.0.1.

## Status and limits

| | Status |
|---|---|
| Claude Code 2.1 | tested end to end: record, deny, `ask` (Claude shows its own approval prompt with metacog's reason), stop-and-retry, egress, deploy re-check |
| Muse Code 1.4 | tested end to end, including the deploy re-check. Muse's hook format is not documented; metacog uses the Claude-compatible shape, verified by running it. Muse's shell sandbox defaults to `--sandbox-network proxy-only`, so reaching a local kind API needs `--sandbox-network enabled` |
| Codex | hooks written from the Codex docs, **not tested yet** |

- Hooks see the agent's *tool calls*. They do not see what the agent binary itself sends; the egress map covers that only for programs that honor `HTTPS_PROXY`.
- The egress proxy also applies to commands the agent runs, so a request to an unresolvable host fails with a proxy `502` instead of a DNS error.
- In a headless run started with `metacog wrap muse exec`, an `ask` becomes a deny with the reason, because Muse would otherwise wait forever for an approval nobody can give. This also stops routine steps such as `kind load docker-image`. (`claude -p` already treats an unanswerable `ask` as a block.)
- The guard is pattern-based and strips quotes before matching, so text such as `git commit -m "docker push fix"` can trigger a rule. It raises the cost of mistakes and obvious exfiltration; it is not a sandbox. Codex documents its own hooks the same way.
- Verification adds about 2–10 s at the end of a turn, plus up to `kube_wait` seconds when the deploy re-check runs.

## Scope changes since the application

- **MCP proxy dropped.** The plan had a stdio proxy in front of MCP servers. Hooks already receive MCP tool calls (`mcp__*`), so the proxy would have recorded nothing new.
- **Dashboard approval button replaced** by the agent's own `ask` prompt, which Claude Code shows in the terminal with metacog's reason. One approval surface instead of two.
- **Kubernetes re-check became opt-in** (`kube_contexts`), so a default install never talks to a cluster.

## Built during the Meta Global AI Developer Hackathon

Everything in this repository was written during the hackathon (Oct 26 – Nov 4, 2026); the commit history is the record. External pieces: Claude Code, Muse Code and Codex are the agents being supervised, and Meta Muse Spark (via the Meta Model API) is the verifier model.

## License

Apache-2.0
