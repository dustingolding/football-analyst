#!/usr/bin/env bash
# Build the web and pipeline images, load them into k3s (no registry), and roll out.
#
#   ./deploy.sh
#
# One-time setup already done on this server: k3s installed, k8s/traefik-config.yaml applied,
# and the web-db / pipeline-env Secrets created in the football-analyst namespace.
set -euo pipefail

cd "$(dirname "$0")"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

TAG="$(date -u +%Y%m%d-%H%M%S)"
KEEP_IMAGES=3

build() {  # build <name> <dockerfile>
    local image="football-analyst-$1:${TAG}"
    echo "Building ${image}..."
    sudo docker build -q -f "$2" -t "$image" . >/dev/null
    echo "Importing ${image} into k3s..."
    sudo docker save "$image" | sudo k3s ctr images import - >/dev/null
}

build web Dockerfile
build pipeline Dockerfile.pipeline

echo "Deploying..."
sed -i -E "s|^([[:space:]]*image: )football-analyst-web:[^[:space:]]+|\1football-analyst-web:${TAG}|" k8s/app.yaml
sed -i -E "s|^([[:space:]]*image: )football-analyst-pipeline:[^[:space:]]+|\1football-analyst-pipeline:${TAG}|" k8s/cronjobs.yaml k8s/live.yaml
kubectl apply -f k8s/namespace.yaml -f k8s/app.yaml -f k8s/ingress.yaml -f k8s/cronjobs.yaml -f k8s/live.yaml
kubectl -n football-analyst rollout status deployment/web --timeout=120s
kubectl -n football-analyst rollout status deployment/live --timeout=120s

echo "Pruning old images (keeping ${KEEP_IMAGES} of each)..."
for name in web pipeline; do
    sudo docker images --format '{{.Repository}}:{{.Tag}}' "football-analyst-${name}" | sort -r | tail -n +$((KEEP_IMAGES + 1)) \
        | while read -r old; do
            sudo docker rmi "$old" >/dev/null || true
            sudo k3s ctr images rm "docker.io/library/${old}" >/dev/null 2>&1 || true
        done
done

echo "Deployed ${TAG}"
kubectl -n football-analyst get cronjobs
