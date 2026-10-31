"""Re-check deploy claims against the live cluster after the agent stops.

A deploy can be "successful" in the agent's own log (`kubectl apply` exit 0, pods Running)
and broken a few seconds later (CrashLoopBackOff). Comparing the report with the log cannot
catch that, so metacog looks at the cluster itself.

Safety: read-only `kubectl get pods` only, and only on contexts listed in config
"kube_contexts" (empty by default = feature off). Anything else is skipped, never queried.
"""

import json
import os
import re
import subprocess
import time

DEPLOY_CLAIM = re.compile(r"\b(deploy(ed|s)?|roll(ed)?[ -]?out|released|redeployed|(is|are) (up|running|healthy|live|ready)|"
                          r"pods? (is|are) (running|ready|healthy))\b", re.I)
KUBE_CMD = re.compile(r"\b(kubectl|helm)\b")
CONTEXT_FLAG = re.compile(r"--(?:kube-)?context[= ](\S+)")
NS_FLAG = re.compile(r"(?:\s-n|--namespace)[= ](\S+)")
BAD_REASONS = {"CrashLoopBackOff", "Error", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "OOMKilled", "RunContainerError"}


def _kubectl(args: list[str], cfg: dict, timeout: float = 15) -> tuple[bool, str]:
    env = dict(os.environ)
    if cfg.get("kubeconfig"):
        env["KUBECONFIG"] = os.path.expanduser(cfg["kubeconfig"])
    try:
        p = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return p.returncode == 0, p.stdout if p.returncode == 0 else p.stderr


def targets(results: list[dict], cfg: dict) -> set[tuple[str, str]]:
    """(context, namespace) pairs the agent touched with kubectl/helm."""
    out = set()
    for r in results:
        cmd = r.get("command") or ""
        if r.get("kind") != "shell" or not KUBE_CMD.search(cmd):
            continue
        m = CONTEXT_FLAG.search(cmd)
        ctx = m.group(1) if m else None
        if ctx is None:
            ok, cur = _kubectl(["config", "current-context"], cfg)
            ctx = cur.strip() if ok else "?"
        ns = NS_FLAG.search(cmd)
        out.add((ctx.strip("'\""), ns.group(1).strip("'\"") if ns else "default"))
    return out


def pod_problems(pods_json: str) -> tuple[list[str], list[str]]:
    """(problem lines, all pod lines) from `kubectl get pods -o json`."""
    problems, lines = [], []
    for pod in json.loads(pods_json).get("items", []):
        name = pod["metadata"]["name"]
        for cs in pod.get("status", {}).get("containerStatuses", []) or []:
            state = cs.get("state", {})
            reason = (state.get("waiting") or state.get("terminated") or {}).get("reason") or ("Running" if "running" in state else "?")
            last = (cs.get("lastState", {}).get("terminated") or {}).get("reason")
            line = f"{name}/{cs['name']}: {reason}, ready={cs.get('ready')}, restarts={cs.get('restartCount', 0)}" + (f", last exit: {last}" if last else "")
            lines.append(line)
            if reason in BAD_REASONS or cs.get("restartCount", 0) > 0 or last in BAD_REASONS:
                problems.append(line)
    return problems, lines


def recheck(message: str, results: list[dict], cfg: dict) -> list[dict]:
    allowed = cfg.get("kube_contexts") or []
    if not allowed or not DEPLOY_CLAIM.search(message):
        return []
    entries = []
    for ctx, ns in sorted(targets(results, cfg)):
        if ctx not in allowed:
            entries.append({"event": "Recheck", "tool": "metacog-recheck", "kind": "note", "ok": True,
                            "command": f"(skipped) context {ctx}", "output": "context is not on the kube_contexts allow list; not queried"})
            continue
        deadline = time.time() + cfg.get("kube_wait", 25)
        while True:
            ok, out = _kubectl(["--context", ctx, "-n", ns, "get", "pods", "-o", "json"], cfg)
            problems, lines = pod_problems(out) if ok else ([f"kubectl failed: {out.strip()[:300]}"], [])
            if problems or time.time() >= deadline:
                break
            time.sleep(5)  # crashes often show up seconds after the agent saw "Running"
        entries.append({"event": "Recheck", "tool": "metacog-recheck", "kind": "shell", "ok": not problems,
                        "command": f"kubectl --context {ctx} -n {ns} get pods", "output": "\n".join(problems or lines) or "no pods"})
    return entries
