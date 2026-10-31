"""Pre-execution guard: block destructive commands and data exfiltration before they run.

Returns {"decision": "deny"|"ask"|"flag", "rule": ..., "reason": ...} or None.
"flag" is recorded but lets the call through (e.g. reading a secret), so a later network
call in the same session can be judged in that light.
"""

import re

SECRET_PATHS = re.compile(
    r"(~|\$HOME|/Users/[^/\s]+|/home/[^/\s]+)?/?\.(ssh|aws|gnupg|kube|docker)/|"
    r"(^|[\s/'\"=])\.env(\.[\w-]+)?\b|\bid_(rsa|ed25519|ecdsa)\b|\.netrc\b|\.npmrc\b|\.pypirc\b|"
    r"credentials(\.json)?\b|\.git-credentials\b|\bkeychain\b|security\s+find-(generic|internet)-password|"
    r"\.(bash|zsh)_history\b|\bprintenv\b|\benv\s*$|/etc/shadow\b"
)
NET_TOOLS = re.compile(r"\b(curl|wget|nc|ncat|netcat|scp|sftp|rsync|ftp|telnet|socat|http|httpie)\b|"
                       r"requests\.(post|put)|urllib\.request|fetch\(|Invoke-WebRequest|/dev/tcp/")
UPLOAD = re.compile(r"(\s-d\b|\s--data(-\w+)?\b|\s-F\b|\s--form\b|\s-T\b|\s--upload-file\b|\s-X\s*(POST|PUT)\b|"
                    r"--post-(data|file)\b|\bscp\b|\brsync\b.*:|\bnc\b|/dev/tcp/|requests\.(post|put))")
HOST = re.compile(r"(?:https?|ftp|wss?)://([^/\s:'\"]+)|(?:\S+@)?([a-zA-Z0-9.-]+\.[a-zA-Z]{2,}):\S*")
ENCODE = re.compile(r"\b(base64|xxd|gzip|tar\s+-?c|zip|openssl\s+enc)\b")
SUPERVISOR_FILES = re.compile(r"\.claude/settings(\.local)?\.json|\.muse/hooks\.json|\.config/muse/settings\.json|"
                              r"\.codex/(hooks\.json|config\.toml)|\.metacog/")

# (rule, pattern, decision, reason) checked against shell commands
COMMAND_RULES = [
    ("rm-root", re.compile(r"\brm\s+-[a-zA-Z]*(rf|fr)[a-zA-Z]*\s+(--no-preserve-root\s+)?(/|~|\$HOME)(\s|/?\*|/?$)"),
     "deny", "recursive delete of the root or home directory"),
    ("disk-wipe", re.compile(r"\b(mkfs(\.\w+)?|dd\s+if=.*\sof=/dev/(disk|sd|nvme))"), "deny", "formats or overwrites a disk"),
    ("pipe-to-shell", re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b"), "ask", "pipes a remote script into a shell"),
    ("force-push", re.compile(r"\bgit\s+push\b.*(\s--force\b|\s-f\b|\s--force-with-lease\b)"), "ask", "force-pushes and can overwrite remote history"),
    ("git-reset-hard", re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f)"), "ask", "discards local changes"),
    ("kubectl-delete", re.compile(r"\bkubectl\b.*\s(delete|drain)\b"), "ask", "deletes Kubernetes resources"),
    ("terraform", re.compile(r"\bterraform\s+(apply|destroy)\b"), "ask", "changes real infrastructure"),
    ("helm-uninstall", re.compile(r"\bhelm\s+(uninstall|delete)\b"), "ask", "removes a Helm release"),
    ("sql-drop", re.compile(r"\b(DROP\s+(TABLE|DATABASE|SCHEMA)|TRUNCATE\s+TABLE)\b", re.I), "ask", "drops or truncates database objects"),
    ("publish", re.compile(r"\b(npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b"), "ask", "publishes a package"),
    ("chmod-777", re.compile(r"\bchmod\s+-R\s+777\b"), "ask", "makes files world-writable"),
]


def hosts(cmd: str) -> list[str]:
    return [(a or b).lower() for a, b in HOST.findall(cmd)]


def allowed(host: str, allow: list[str]) -> bool:
    return any(host == a or host.endswith("." + a) for a in allow)


def read_secrets_before(history: list[dict]) -> list[str]:
    return [r.get("target", "") for r in history
            if r.get("event") == "Guard" and r.get("rule") == "secret-read"]


def check(rec: dict, history: list[dict], cfg: dict) -> dict | None:
    kind, cmd, path = rec.get("kind"), rec.get("command", ""), rec.get("path", "")

    if kind == "write" and SUPERVISOR_FILES.search(path):
        return {"decision": "deny", "rule": "tamper-supervisor", "reason": f"editing {path} would change the agent's own oversight hooks"}
    if kind == "shell" and SUPERVISOR_FILES.search(cmd) and re.search(r"(>|\btee\b|\bsed\s+-i|\brm\b|\bmv\b|\bcp\b)", cmd):
        return {"decision": "deny", "rule": "tamper-supervisor", "reason": "shell command modifies the agent's oversight hooks"}

    if kind == "shell":
        for rule, pat, decision, reason in COMMAND_RULES:
            if pat.search(cmd):
                return {"decision": decision, "rule": rule, "reason": reason}

        net = bool(NET_TOOLS.search(cmd))
        secret = bool(SECRET_PATHS.search(cmd))
        outside = [h for h in hosts(cmd) if not allowed(h, cfg["allow_hosts"])]
        blocked = [h for h in hosts(cmd) if allowed(h, cfg.get("block_hosts", []))]
        if blocked:
            return {"decision": "deny", "rule": "blocked-host", "reason": f"contacts blocked host {', '.join(blocked)}", "hosts": blocked}
        if net and secret:
            return {"decision": "deny", "rule": "exfiltration", "reason": "sends secret material (keys, credentials, env) over the network"}
        if net and UPLOAD.search(cmd) and outside:
            earlier = read_secrets_before(history)
            if earlier or ENCODE.search(cmd):
                why = f"after reading {earlier[-1]}" if earlier else "with encoded payload"
                return {"decision": "deny", "rule": "exfiltration", "reason": f"uploads data to {', '.join(outside)} {why}", "hosts": outside}
            return {"decision": "ask", "rule": "unknown-upload", "reason": f"uploads data to a host not on the allow list: {', '.join(outside)}", "hosts": outside}
        if secret:
            return {"decision": "flag", "rule": "secret-read", "reason": "reads secret material", "target": cmd[:200]}
        if net and outside:
            return {"decision": "flag", "rule": "unknown-host", "reason": f"contacts {', '.join(outside)}", "hosts": outside}

    if kind == "read" and SECRET_PATHS.search(path):
        return {"decision": "flag", "rule": "secret-read", "reason": f"reads secret file {path}", "target": path}
    return None
