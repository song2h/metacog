"""Waste detection: patterns that burn tokens and time without moving the task forward."""

from collections import Counter

BIG_OUTPUT = 20000


def analyze(turn: list[dict]) -> list[dict]:
    done = [r for r in turn if r.get("event") in ("PostToolUse", "PostToolUseFailure")]
    out = []

    reads = Counter(r["path"] for r in done if r.get("kind") == "read" and r.get("path"))
    for path, n in reads.items():
        if n >= 3:
            out.append({"kind": "repeated-read", "count": n, "detail": f"read {path} {n} times",
                        "tip": "keep the relevant part in context or grep for the lines you need"})

    fails = Counter(r.get("command") for r in done if r.get("kind") == "shell" and not r.get("ok") and r.get("command"))
    for cmd, n in fails.items():
        if n >= 2:
            out.append({"kind": "retry-loop", "count": n, "detail": f"`{cmd[:80]}` failed {n} times",
                        "tip": "read the error and change the approach before retrying the same command"})

    big = [r for r in done if r.get("output_len", 0) > BIG_OUTPUT]
    if big:
        total = sum(r["output_len"] for r in big)
        out.append({"kind": "large-output", "count": len(big), "detail": f"{len(big)} tool outputs over {BIG_OUTPUT:,} chars ({total:,} total)",
                    "tip": "pipe through head/tail/grep or use a quieter flag (-q, --silent) to keep context small"})

    same = Counter((r.get("tool"), r.get("command") or r.get("path")) for r in done if r.get("ok"))
    for (tool, target), n in same.items():
        if n >= 3 and tool and target and target not in reads:
            out.append({"kind": "duplicate-call", "count": n, "detail": f"{tool} `{str(target)[:80]}` ran {n} times with success",
                        "tip": "reuse the earlier result instead of running it again"})
    return out
