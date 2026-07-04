#!/usr/bin/env bash
# deploy.sh — Build locally, load into remote k3s, and deploy chatbot
# Usage: ./scripts/deploy.sh
# Optional overrides:
#   OLLAMA_BASE_URL=http://10.0.0.141:11434 KUBECONFIG_PATH=/home/borg/.kube/k3s-remote NODE_SSH_HOST=thunderball ./scripts/deploy.sh

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://10.0.0.141:11434}"
OLLAMA_MODEL="${OLLAMA_MODEL:-phi4-mini}"
KUBECONFIG_PATH="${KUBECONFIG_PATH:-/home/borg/.kube/k3s-remote}"
NODE_SSH_HOST="${NODE_SSH_HOST:-thunderball}"
KUBECTL=(kubectl --kubeconfig "${KUBECONFIG_PATH}")
FRONTEND_IMAGE="chatbot-frontend:latest"
BACKEND_IMAGE="chatbot-backend:latest"
IDENTITY_IMAGE="chatbot-identity:latest"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
K8S_DIR="${REPO_ROOT}/k8s"

echo "================================================"
echo " Chatbot Deploy Script"
echo " Ollama   : ${OLLAMA_BASE_URL}"
echo " Kubeconf : ${KUBECONFIG_PATH}"
echo " Node SSH : ${NODE_SSH_HOST}"
echo "================================================"
echo ""

ensure_ollama_watchdog() {
        local wd_script="/tmp/ollama-watchdog.sh"
        local wd_service="/tmp/ollama-watchdog.service"
        local wd_timer="/tmp/ollama-watchdog.timer"

        cat > "${wd_script}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
MAX_TIME="${MAX_TIME:-120}"

healthcheck() {
    # Lightweight liveness probe to avoid competing with user chat generation.
    curl -fsS --max-time "${MAX_TIME}" "${OLLAMA_URL}/api/tags" | grep -q '"models"'
}

if healthcheck; then
    logger -t ollama-watchdog "health check passed"
    exit 0
fi

logger -t ollama-watchdog "health check failed; restarting ollama"
systemctl restart ollama
sleep 3

if healthcheck; then
    logger -t ollama-watchdog "health check passed after restart"
    exit 0
fi

logger -t ollama-watchdog "health check still failing after restart"
exit 1
EOF

        cat > "${wd_service}" <<'EOF'
[Unit]
Description=Ollama watchdog health check and recovery
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=oneshot
Environment="OLLAMA_URL=http://127.0.0.1:11434"
Environment="MAX_TIME=120"
ExecStart=/usr/local/bin/ollama-watchdog.sh
EOF

        cat > "${wd_timer}" <<'EOF'
[Unit]
Description=Run Ollama watchdog periodically

[Timer]
OnBootSec=2m
OnUnitActiveSec=5m
AccuracySec=15s
Unit=ollama-watchdog.service
Persistent=true

[Install]
WantedBy=timers.target
EOF

        echo "▶ Ensuring Ollama watchdog is installed on ${NODE_SSH_HOST}..."
        scp "${wd_script}" "${wd_service}" "${wd_timer}" "${NODE_SSH_HOST}:/tmp/"
        ssh -t "${NODE_SSH_HOST}" "sudo install -m 755 /tmp/ollama-watchdog.sh /usr/local/bin/ollama-watchdog.sh && sudo install -m 644 /tmp/ollama-watchdog.service /etc/systemd/system/ollama-watchdog.service && sudo install -m 644 /tmp/ollama-watchdog.timer /etc/systemd/system/ollama-watchdog.timer && sudo systemctl daemon-reload && sudo systemctl enable --now ollama-watchdog.timer"

        rm -f "${wd_script}" "${wd_service}" "${wd_timer}"
}
ensure_ollama_watchdog

ensure_ollama_model() {
	echo "▶ Ensuring Ollama model ${OLLAMA_MODEL} exists on ${NODE_SSH_HOST}..."
	ssh "${NODE_SSH_HOST}" "ollama show '${OLLAMA_MODEL}' >/dev/null 2>&1 || ollama pull '${OLLAMA_MODEL}'"
}

ensure_ollama_model

probe_ollama_chat() {
	ssh "${NODE_SSH_HOST}" "curl -fsS --max-time 120 -H 'Content-Type: application/json' -X POST '${OLLAMA_BASE_URL}/api/chat' -d '{\"model\":\"${OLLAMA_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the single word: pong\"}],\"stream\":false,\"options\":{\"num_predict\":8}}' | grep -q '\"done\":true'"
}

echo "▶ Checking Ollama chat health on ${NODE_SSH_HOST}..."
if probe_ollama_chat; then
    echo "✓ Ollama chat health check passed"
else
    echo "⚠ Ollama health check failed. Restarting Ollama service..."
    ssh -t "${NODE_SSH_HOST}" 'sudo systemctl restart ollama'
    echo "▶ Rechecking Ollama chat health..."
    if ! probe_ollama_chat; then
        echo "✖ Ollama is still unhealthy after restart. Fix Ollama on ${NODE_SSH_HOST} and rerun deploy."
        exit 1
    fi
    echo "✓ Ollama recovered after restart"
fi

# ── Build images locally ───────────────────────────────────────────────────────
echo ""
echo "▶ Building frontend image..."
docker build -t "${FRONTEND_IMAGE}" "${REPO_ROOT}/frontend"

echo ""
echo "▶ Building backend image..."
docker build -t "${BACKEND_IMAGE}" "${REPO_ROOT}/backend"

echo ""
echo "▶ Building identity image..."
docker build -t "${IDENTITY_IMAGE}" "${REPO_ROOT}/identity"

# ── Load images into remote k3s runtime (no registry) ─────────────────────────
echo ""
echo "▶ Loading images into k3s on ${NODE_SSH_HOST}..."
LOCAL_ARCHIVE="$(mktemp /tmp/chatbot-images-XXXXXX.tar)"
REMOTE_ARCHIVE="/tmp/$(basename "${LOCAL_ARCHIVE}")"

echo "▶ Creating local image archive..."
docker save -o "${LOCAL_ARCHIVE}" "${FRONTEND_IMAGE}" "${BACKEND_IMAGE}" "${IDENTITY_IMAGE}"

echo "▶ Copying archive to ${NODE_SSH_HOST}..."
scp "${LOCAL_ARCHIVE}" "${NODE_SSH_HOST}:${REMOTE_ARCHIVE}"
rm -f "${LOCAL_ARCHIVE}"

echo "▶ Importing images into k3s (sudo may prompt once)..."
ssh -t "${NODE_SSH_HOST}" "sudo sh -c 'k3s ctr images import \"${REMOTE_ARCHIVE}\" && rm -f \"${REMOTE_ARCHIVE}\"'"

# ── Deploy to Kubernetes ───────────────────────────────────────────────────────
echo ""
echo "▶ Applying Kubernetes manifests..."

# Namespace first
"${KUBECTL[@]}" apply -f "${K8S_DIR}/namespace.yaml"
"${KUBECTL[@]}" apply -f "${K8S_DIR}/identity-namespace.yaml"

# Apply remaining manifests with OLLAMA_BASE_URL substituted
for manifest in backend.yaml frontend.yaml cloudflared.yaml; do
	sed -e "s|__OLLAMA_BASE_URL__|${OLLAMA_BASE_URL}|g" \
	-e "s|__OLLAMA_MODEL__|${OLLAMA_MODEL}|g" \
		"${K8S_DIR}/${manifest}" | "${KUBECTL[@]}" apply -f -
done

"${KUBECTL[@]}" apply -f "${K8S_DIR}/identity-mongodb.yaml"
"${KUBECTL[@]}" apply -f "${K8S_DIR}/identity-auth.yaml"
"${KUBECTL[@]}" apply -f "${K8S_DIR}/identity-networkpolicy.yaml"

# Force app pods to pick up freshly imported local images (latest tag).
"${KUBECTL[@]}" -n chatbot rollout restart deployment/chatbot-backend
"${KUBECTL[@]}" -n chatbot rollout restart deployment/chatbot-frontend
"${KUBECTL[@]}" -n chatbot rollout restart deployment/chatbot-cloudflared
"${KUBECTL[@]}" -n identity rollout restart deployment/identity-api

# ── Wait for rollout ───────────────────────────────────────────────────────────
echo ""
echo "▶ Waiting for deployments to be ready..."
"${KUBECTL[@]}" rollout status deployment/chatbot-backend   -n chatbot --timeout=120s
"${KUBECTL[@]}" rollout status deployment/chatbot-frontend  -n chatbot --timeout=120s
"${KUBECTL[@]}" rollout status deployment/chatbot-cloudflared -n chatbot --timeout=60s
"${KUBECTL[@]}" rollout status statefulset/mongodb -n identity --timeout=180s
"${KUBECTL[@]}" rollout status deployment/identity-api -n identity --timeout=180s

# ── Deployment summary ─────────────────────────────────────────────────────────
echo ""
echo "================================================"
echo " Deployment complete!"
echo "================================================"
echo ""
echo "Cloudflared deployed as a named tunnel at chat.xgalvanize.ca"
echo "Routing comes from k8s/cloudflared-config.yaml plus a Cloudflare DNS CNAME."
echo ""
echo "To inspect tunnel logs:"
echo "  kubectl --kubeconfig ${KUBECONFIG_PATH} logs -n chatbot -l app=chatbot-cloudflared -f"
echo ""
echo "To check pod status:"
echo "  kubectl --kubeconfig ${KUBECONFIG_PATH} get pods -n chatbot"