#!/usr/bin/env bash
set -euo pipefail

# Port-forward MongoDB from the identity namespace to localhost.
# Defaults:
#   LOCAL_PORT=27017
#   REMOTE_PORT=27017
#   RESOURCE=pod/mongodb-0
#   NAMESPACE=identity
#   KUBECONFIG_PATH=/home/borg/.kube/k3s-remote

LOCAL_PORT="${LOCAL_PORT:-27017}"
REMOTE_PORT="${REMOTE_PORT:-27017}"
RESOURCE="${RESOURCE:-pod/mongodb-0}"
#NAMESPACE="${NAMESPACE:-identity}"
KUBECONFIG_PATH="${KUBECONFIG_PATH:-/home/borg/.kube/k3s-remote}"

if command -v ss >/dev/null 2>&1; then
  if ss -ltn "( sport = :${LOCAL_PORT} )" | tail -n +2 | grep -q .; then
    echo "Local port ${LOCAL_PORT} is already in use."
    echo "Stop the process using it or pick another local port:"
    echo "  LOCAL_PORT=27018 $0"
    exit 1
  fi
fi

echo "Starting port-forward: ${RESOURCE} ${LOCAL_PORT}:${REMOTE_PORT} (namespace: ${NAMESPACE})"
echo "Using kubeconfig: ${KUBECONFIG_PATH}"
echo "Press Ctrl+C to stop."

kubectl --kubeconfig "${KUBECONFIG_PATH}" -n "${NAMESPACE}" port-forward "${RESOURCE}" "${LOCAL_PORT}:${REMOTE_PORT}"
