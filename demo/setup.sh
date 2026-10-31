#!/usr/bin/env bash
# Demo cluster for the deploy re-check. Touches ONLY the kind cluster in its own kubeconfig.
set -euo pipefail
export KUBECONFIG="${METACOG_DEMO_KUBECONFIG:-$HOME/.metacog/demo-kubeconfig}"
CTX=kind-metacog-demo
kind get clusters | grep -qx metacog-demo || kind create cluster --name metacog-demo --kubeconfig "$KUBECONFIG"
[ "$(kubectl config current-context)" = "$CTX" ] || { echo "refusing: current context is not $CTX" >&2; exit 1; }
for v in v1 v2; do
  docker build -q --build-arg VERSION=$v -t metacog-demo/api:$v "$(dirname "$0")/image" >/dev/null
  kind load docker-image metacog-demo/api:$v --name metacog-demo >/dev/null
done
kubectl --context $CTX create namespace shop --dry-run=client -o yaml | kubectl --context $CTX apply -f -
kubectl --context $CTX -n shop delete deploy api --ignore-not-found --wait
kubectl --context $CTX apply -f "$(dirname "$0")/api-v1.yaml"
kubectl --context $CTX -n shop rollout status deploy/api --timeout=90s
echo "ready: KUBECONFIG=$KUBECONFIG, context $CTX, namespace shop running api v1"
