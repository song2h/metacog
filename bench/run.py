"""Measure the verifier: detection rate on false reports, false-positive rate on honest ones.

    uv run python bench/run.py            # all cases, real Meta Model API calls
    uv run python bench/run.py --repeat 3 # run each case 3 times to see variance
    uv run python bench/run.py --deterministic  # ablation: no model, code checks only

"Detected" means Metacog would send the report back to the agent (at least one problem).
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cases import CASES  # noqa: E402

from metacog import store, verify  # noqa: E402


def load_cases() -> list[dict]:
    recorded = [json.loads(p.read_text()) for p in sorted((Path(__file__).parent / "recorded").glob("*.json"))]
    return CASES + recorded


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out", default=None, help="write per-case results as JSON")
    ap.add_argument("--deterministic", action="store_true", help="no model call: measure the code checks alone")
    a = ap.parse_args()
    if a.deterministic:
        verify.llm.chat_json = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("deterministic-only run"))
    cfg = store.config()
    rows = []
    for case in load_cases():
        for n in range(a.repeat):
            t0 = time.time()
            r = verify.run(case["message"], case["log"], "", cfg)
            rows.append({"name": case["name"], "label": case["label"], "run": n, "detected": bool(r["problems"]),
                         "warned": any(f["severity"] in ("warn", "unverifiable") for f in r["findings"]),
                         "latency_s": round(time.time() - t0, 2), "tokens": r["usage"].get("total_tokens", 0),
                         "prompt_tokens": r["usage"].get("prompt_tokens", 0), "completion_tokens": r["usage"].get("completion_tokens", 0),
                         "error": r["error"], "problems": [p.get("claim") or p.get("detail") for p in r["problems"]]})
            mark = "✓" if rows[-1]["detected"] == (case["label"] == "lie") else "✗"
            err = r["error"] if r["error"] and not a.deterministic else ""
            print(f"{mark} {case['label']:<6} {case['name']:<28} detected={rows[-1]['detected']!s:<5} {rows[-1]['latency_s']:>5}s"
                  f"{'  ERROR ' + err if err else ''}")

    lies = [r for r in rows if r["label"] == "lie"]
    honest = [r for r in rows if r["label"] == "honest"]
    ok = [r for r in rows if not r["error"]] if not a.deterministic else []
    # Muse Spark 1.3 standard pricing per 1M tokens (dev.meta.ai pricing page, 2026-10-31): $1.25 in, $4.25 out
    cost = [(r["prompt_tokens"] * 1.25 + r["completion_tokens"] * 4.25) / 1e6 for r in ok]
    print(f"\nmodel {'none (deterministic only)' if a.deterministic else cfg['model']}  cases {len(load_cases())} x {a.repeat}")
    print(f"detection rate (lies caught)      {sum(r['detected'] for r in lies)}/{len(lies)}")
    print(f"false positives (honest flagged)  {sum(r['detected'] for r in honest)}/{len(honest)}")
    print(f"warnings only (lies / honest)     {sum(r['warned'] and not r['detected'] for r in lies)}/{len(lies)}"
          f" / {sum(r['warned'] and not r['detected'] for r in honest)}/{len(honest)}")
    if ok:
        print(f"latency median {statistics.median(r['latency_s'] for r in ok)}s  max {max(r['latency_s'] for r in ok)}s")
        print(f"cost per verification median ${statistics.median(cost):.4f}  total ${sum(cost):.4f}")
    if a.out:
        Path(a.out).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
