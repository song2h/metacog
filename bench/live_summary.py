"""Summarize live deploy trials (demo/trials.sh) and export the evidence with local paths removed.

    uv run python bench/live_summary.py /tmp/trials --since 2026-11-01T16:55 --out bench/results/live/2026-11-01.jsonl

For each run directory (<agent>-<n>/sessions/*.jsonl) it keeps the final reports and verdicts written after --since,
and counts: first report false, sent back, and whether a later report was accepted with no false claims.
"""

import argparse
import json
import re
import time
from pathlib import Path

LOCAL = re.compile(r"(/private)?/var/folders/[^\s`'\")\]]+|/Users/[^/\s]+|/tmp/[^\s`'\")\]]+")


def redact(text: str) -> str:
    return LOCAL.sub("<local>", text or "")


def run_records(run_dir: Path, since: float) -> list[dict]:
    recs = []
    for f in sorted((run_dir / "sessions").glob("*.jsonl")):
        recs += [r for r in map(json.loads, f.read_text().splitlines()) if r.get("ts", 0) >= since]
    return sorted((r for r in recs if r.get("event") in ("Stop", "Verdict")), key=lambda r: r["ts"])


def outcome(recs: list[dict]) -> dict:
    verdicts = [r for r in recs if r["event"] == "Verdict"]
    false = [any(c["verdict"] == "false" for c in v.get("claims", [])) for v in verdicts]
    return {"reports": len(verdicts), "first_false": bool(false and false[0]), "sent_back": any(v.get("blocked") for v in verdicts),
            "corrected": bool(false and false[0] and not false[-1]), "released_false": bool(false and false[-1] and len(false) > 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trials", type=Path)
    ap.add_argument("--since", required=True, help="local time, e.g. 2026-11-01T16:55")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    since = time.mktime(time.strptime(a.since, "%Y-%m-%dT%H:%M"))
    rows, export = [], []
    for run in sorted(p for p in a.trials.iterdir() if (p / "sessions").is_dir()):
        recs = run_records(run, since)
        if not recs:
            continue
        o = {"run": run.name, **outcome(recs)}
        rows.append(o)
        print(f"{o['run']:<10} reports={o['reports']} first_false={o['first_false']!s:<5} sent_back={o['sent_back']!s:<5} "
              f"corrected={o['corrected']!s:<5} released_false={o['released_false']}")
        for r in recs:
            item = {"run": run.name, "event": r["event"], "ts": round(r["ts"])}
            if r["event"] == "Stop":
                item["message"] = redact(r.get("message"))
            else:
                item.update(blocked=r.get("blocked"), model=r.get("model"), same_family=r.get("same_family"),
                            claims=[{k: redact(c.get(k, "")) if isinstance(c.get(k), str) else c.get(k) for k in ("verdict", "claim", "explanation")}
                                    for c in r.get("claims", [])],
                            rechecks=[redact(x.get("output", ""))[:600] for x in r.get("rechecks", [])])
            export.append(item)
    for agent in sorted({r["run"].rsplit("-", 1)[0] for r in rows}):
        rs = [r for r in rows if r["run"].startswith(agent + "-")]
        print(f"{agent}: runs {len(rs)}, first report false {sum(r['first_false'] for r in rs)}, sent back {sum(r['sent_back'] for r in rs)}, "
              f"corrected {sum(r['corrected'] for r in rs)}, false report released {sum(r['released_false'] for r in rs)}")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in export))


if __name__ == "__main__":
    main()
