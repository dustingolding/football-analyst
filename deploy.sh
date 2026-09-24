#!/usr/bin/env bash
# Build the web image, load it into k3s (no registry), and roll out.
#
#   ./deploy.sh
#
# One-time setup already done on this server: k3s installed, k8s/traefik-config.yaml applied,
# and the web-db Secret created in the football-analyst namespace.
set -euo pipefail

cd "$(dirname "$0")"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

TAG="$(date -u +%Y%m%d-%H%M%S)"
IMAGE="football-analyst-web:${TAG}"
KEEP_IMAGES=3

echo "Building ${IMAGE}..."
sudo docker build -t "$IMAGE" .

echo "Importing into k3s..."
sudo docker save "$IMAGE" | sudo k3s ctr images import -

echo "Deploying..."
sed -i -E "s|^([[:space:]]*image: )football-analyst-web:[^[:space:]]+|\1${IMAGE}|" k8s/app.yaml
kubectl apply -f k8s/namespace.yaml -f k8s/app.yaml -f k8s/ingress.yaml
kubectl -n football-analyst rollout status deployment/web --timeout=120s

echo "Pruning old images (keeping ${KEEP_IMAGES})..."
sudo docker images --format '{{.Repository}}:{{.Tag}}' football-analyst-web | sort -r | tail -n +$((KEEP_IMAGES + 1)) \
    | while read -r old; do
        sudo docker rmi "$old" >/dev/null || true
        sudo k3s ctr images rm "docker.io/library/${old}" >/dev/null 2>&1 || true
    done

echo "Deployed ${IMAGE}"
