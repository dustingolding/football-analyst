#!/usr/bin/env bash
# Build the web and pipeline images from this checkout, load them into k3s (no registry), and
# roll out one environment.
#
#   ./deploy.sh dev     # from ~/football-analyst          -> dev.sidelinewire.com
#   ./deploy.sh prod    # from ~/football-analyst-prod (main) -> sidelinewire.com
#
# Settings per environment live in deploy/<env>.env; manifests in k8s/ are templates filled in
# with envsubst. Each environment's data/ and models/ are this checkout's folders.
# One-time setup per environment: namespace Secrets web-db and pipeline-env (see README notes).
set -euo pipefail

ENV_NAME="${1:-}"
if [[ "$ENV_NAME" != "dev" && "$ENV_NAME" != "prod" ]]; then
    echo "usage: $0 dev|prod" >&2
    exit 2
fi
cd "$(dirname "$0")"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

set -a
# shellcheck disable=SC1090
. "deploy/${ENV_NAME}.env"
set +a

if [[ -n "${REQUIRE_BRANCH:-}" ]]; then
    branch="$(git rev-parse --abbrev-ref HEAD)"
    if [[ "$branch" != "$REQUIRE_BRANCH" || -n "$(git status --porcelain --untracked-files=no)" ]]; then
        echo "ERROR: ${ENV_NAME} deploys from a clean '${REQUIRE_BRANCH}' checkout (this is '${branch}')." >&2
        exit 1
    fi
fi

for secret in web-db pipeline-env; do
    if ! kubectl -n "$NAMESPACE" get secret "$secret" >/dev/null 2>&1; then
        echo "ERROR: Secret ${secret} is missing in namespace ${NAMESPACE}." >&2
        exit 1
    fi
done

TAG="${ENV_NAME}-$(date -u +%Y%m%d-%H%M%S)"
KEEP_IMAGES=3
export APP_DIR="$PWD"
export WEB_IMAGE="football-analyst-web:${TAG}"
export PIPELINE_IMAGE="football-analyst-pipeline:${TAG}"

build() {  # build <image> <dockerfile>
    echo "Building $1..."
    sudo docker build -q -f "$2" -t "$1" . >/dev/null
    echo "Importing $1 into k3s..."
    sudo docker save "$1" | sudo k3s ctr images import - >/dev/null
}
build "$WEB_IMAGE" Dockerfile
build "$PIPELINE_IMAGE" Dockerfile.pipeline

echo "Deploying ${ENV_NAME} (${SITE_HOST}, namespace ${NAMESPACE})..."
kubectl apply -f k8s/traefik-config.yaml >/dev/null
VARS='$NAMESPACE $SITE_HOST $ENTRYPOINT $SITE_ENV $APP_DIR $WEB_IMAGE $PIPELINE_IMAGE $DAILY_SCHEDULE $WEEKLY_SCHEDULE $REFRESH_GAMEDAY_SCHEDULE $REFRESH_HOURLY_SCHEDULE $NEWSROOM_SCHEDULE $NEWSROOM_SUSPEND $NEWSROOM_AUTOPUBLISH $SUPPORT_EMAIL'
for manifest in namespace app ingress cronjobs live notify newsroom; do
    envsubst "$VARS" < "k8s/${manifest}.yaml" | kubectl apply -f -
done
kubectl -n "$NAMESPACE" rollout status deployment/web --timeout=120s
kubectl -n "$NAMESPACE" rollout status deployment/live --timeout=120s
kubectl -n "$NAMESPACE" rollout status deployment/notify --timeout=120s
kubectl -n "$NAMESPACE" rollout status deployment/newsroom-worker --timeout=120s

echo "Pruning old ${ENV_NAME} images (keeping ${KEEP_IMAGES} of each)..."
for name in web pipeline; do
    sudo docker images --format '{{.Repository}}:{{.Tag}}' "football-analyst-${name}" | grep ":${ENV_NAME}-" \
        | sort -r | tail -n +$((KEEP_IMAGES + 1)) \
        | while read -r old; do
            sudo docker rmi "$old" >/dev/null || true
            sudo k3s ctr images rm "docker.io/library/${old}" >/dev/null 2>&1 || true
        done
done

echo "Deployed ${TAG}"
