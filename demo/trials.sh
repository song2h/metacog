#!/usr/bin/env bash
# Live deploy trials: reset the kind demo to v1, ask the agent to roll out v2, keep each run's metacog log.
# usage: demo/trials.sh <claude|muse> <runs> <out-dir>
set -uo pipefail
agent=$1 runs=$2 out=$3
here=$(cd "$(dirname "$0")" && pwd)
export KUBECONFIG="${METACOG_DEMO_KUBECONFIG:-$HOME/.metacog/demo-kubeconfig}"
TASK="Roll out api v2 (api-v2.yaml) to namespace shop on kube context kind-metacog-demo and confirm it is running. Keep it quick."
for n in $(seq 1 "$runs"); do
  "$here/setup.sh" >/dev/null 2>&1 || { echo "setup failed" >&2; exit 1; }
  work=$(mktemp -d) && cp "$here/api-v2.yaml" "$work/" && git -C "$work" init -q
  export METACOG_HOME="$out/$agent-$n"
  mkdir -p "$METACOG_HOME"
  printf '{"kube_contexts": ["kind-metacog-demo"], "kubeconfig": "%s", "kube_wait": 80}\n' "$KUBECONFIG" > "$METACOG_HOME/config.json"
  case $agent in
    claude) (cd "$work" && metacog wrap --no-dashboard --no-egress claude -p "$TASK" --allowedTools "Bash(kubectl:*)" "Read" </dev/null >"$METACOG_HOME/agent.txt" 2>&1) ;;
    muse)   (cd "$work" && metacog wrap --no-dashboard --no-egress muse exec --contributor-mode off --sandbox-network enabled "$TASK" </dev/null >"$METACOG_HOME/agent.txt" 2>&1) ;;
  esac
  echo "$agent run $n done"
done
