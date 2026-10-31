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
from datetime import datetime

# Broad on purpose: a false trigger costs one read-only `kubectl get pods` on an allow-listed context.
DEPLOY_CLAIM = re.compile(r"\b(deploy\w*|roll(ed|s|ing)?[ -]?out|released?|redeployed|running|ready|healthy|available|live)\b", re.I)
KUBE_CMD = re.compile(r"\b(kubectl|helm)\b")
FILE_FLAG = re.compile(r"(?:\s-f|--filename)[= ]?(\S+)")
MANIFEST_NS = re.compile(r"^\s*namespace:\s*['\"]?([\w.-]+)", re.M)
KUSTOMIZE = re.compile(r"\s(-k|--kustomize)\b")
ALL = "*"  # namespace unknown: look at pods created since the change in every namespace
SEPARATOR = re.compile(r"&&|\|\||[;|\n]")
# Only commands that change what runs; `kubectl get ns` alone is not a deploy.
MUTATING = re.compile(r"\bkubectl\b.*\s(apply|create|replace|patch|edit|set|scale|rollout\s+(restart|undo|resume))\b|"
                      r"\bhelm\s+(install|upgrade|rollback)\b")
CONTEXT_FLAG = re.compile(r"--(?:kube-)?context[= ](\S+)")
NS_FLAG = re.compile(r"(?:\s-n[=\s]*|--namespace[= ])([^\s=]\S*)")
BAD_REASONS = {"CrashLoopBackOff", "Error", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "OOMKilled", "RunContainerError"}


def _kubectl(args: list[str], cfg: dict, timeout: float = 15) -> tuple[bool, str]:
    env = dict(os.environ)
    # Muse runs hooks with a cleared environment; look in the usual install locations too.
    env["PATH"] = os.pathsep.join(filter(None, [env.get("PATH"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]))
    if cfg.get("kubeconfig"):
        env["KUBECONFIG"] = os.path.expanduser(cfg["kubeconfig"])
    try:
        p = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return p.returncode == 0, p.stdout if p.returncode == 0 else p.stderr


def _manifest_namespaces(part: str, cwd: str) -> set[str]:
    """Namespaces inside the files a `kubectl apply -f` reads: {"default"} if the files name none,
    {ALL} if metacog cannot read them (stdin, a directory, -k, a path relative to an earlier `cd`)."""
    files = FILE_FLAG.findall(part)
    if KUSTOMIZE.search(part):
        return {ALL}
    if not files:
        return {"default"}
    out = set()
    for f in files:
        path = os.path.join(cwd or ".", os.path.expanduser(f.strip("'\"")))
        try:
            with open(path, errors="replace") as fh:
                out |= set(MANIFEST_NS.findall(fh.read(200_000))) or {"default"}
        except OSError:  # includes directories and "-"
            return {ALL}
    return out


def targets(results: list[dict], cfg: dict, cwd: str = "") -> set[tuple[str, str]]:
    """(context, namespace) pairs the agent changed with kubectl/helm. The namespace comes from -n, else from the
    applied manifest, else `default`. The context comes from --context, else metacog's view of the current context
    (shortcut: an agent's own KUBECONFIG= or `config use-context` is not followed; the allow list still applies)."""
    out, current = set(), None
    for r in results:
        if r.get("kind") != "shell":
            continue
        for part in SEPARATOR.split(r.get("command") or ""):
            if not MUTATING.search(part):
                continue
            m = CONTEXT_FLAG.search(part)
            if m:
                ctx = m.group(1).strip("'\"")
            else:
                if current is None:
                    ok, res = _kubectl(["config", "current-context"], cfg)
                    current = res.strip() if ok else "?"
                ctx = current
            ns = NS_FLAG.search(part)
            for n in ({ns.group(1).strip("'\"")} if ns else _manifest_namespaces(part, cwd)):
                out.add((ctx, n))
    return out


def _created(pod: dict) -> float:
    try:
        return datetime.fromisoformat(pod["metadata"]["creationTimestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return 0.0


def pod_problems(pods_json: str, since: float | None = None, only_new: bool = False) -> tuple[list[str], list[str]]:
    """(problem lines, all pod lines) from `kubectl get pods -o json`. With `since`, only pods created after it
    count when there are any (or always, with only_new): old restarts of untouched pods say nothing about this deploy."""
    pods = [p for p in json.loads(pods_json).get("items", []) if not p["metadata"].get("deletionTimestamp")]  # skip replicas on their way out
    if since and ((new := [p for p in pods if _created(p) >= since]) or only_new):
        pods = new
    elif only_new:
        pods = []
    problems, lines = [], []
    for pod in pods:
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


def recheck(message: str, results: list[dict], cfg: dict, cwd: str = "") -> list[dict]:
    allowed = cfg.get("kube_contexts") or []
    if not allowed or not DEPLOY_CLAIM.search(message):
        return []
    entries = []
    deadline = time.time() + cfg.get("kube_wait", 25)
    changes = [r["ts"] for r in results if r.get("kind") == "shell" and r.get("ts")
               and any(MUTATING.search(p) for p in SEPARATOR.split(r.get("command") or ""))]
    since = min(changes) - 5 if changes else None  # small slack for clock skew with the cluster
    for ctx, ns in sorted(targets(results, cfg, cwd)):
        if ctx not in allowed:
            entries.append({"event": "Recheck", "tool": "metacog-recheck", "kind": "note", "ok": True,
                            "command": f"(skipped) context {ctx}", "output": "context is not on the kube_contexts allow list; not queried"})
            continue
        while True:
            scope = ["-A"] if ns == ALL else ["-n", ns]
            ok, out = _kubectl(["--context", ctx, *scope, "get", "pods", "-o", "json"], cfg)
            problems, lines = pod_problems(out, since, only_new=ns == ALL) if ok else ([], [])
            if not ok or problems or not lines or time.time() >= deadline:
                break
            time.sleep(5)  # crashes often show up seconds after the agent saw "Running"
        cmd = f"kubectl --context {ctx} {' '.join(scope)} get pods"
        if not ok:  # no evidence either way: a note, not a verdict
            entries.append({"event": "Recheck", "tool": "metacog-recheck", "kind": "note", "ok": False, "command": cmd,
                            "output": f"metacog could not query the cluster: {out.strip()[:300]}"})
            continue
        if problems:
            output = ("UNHEALTHY now (checked by metacog after the agent stopped; restarts or a last exit mean the container "
                      "has crashed since it started):\n" + "\n".join(problems))
        else:
            output = "healthy now:\n" + "\n".join(lines) if lines else "no pods"
        entries.append({"event": "Recheck", "tool": "metacog-recheck", "kind": "shell", "ok": not problems, "command": cmd, "output": output})
    return entries
