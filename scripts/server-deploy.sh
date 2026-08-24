#!/usr/bin/env bash
# Run ON the VM (as the cur-web run user, NOT root) to fetch and apply a release.
#
# Usage:   ./server-deploy.sh [tag]
# Default tag: latest. Examples:
#   ./server-deploy.sh            # fetches the latest release
#   ./server-deploy.sh v0.2.0     # fetches a specific tag
#
# Pre-reqs on the VM:
#   - gh CLI installed and authenticated as the current user with a fine-
#     grained PAT (Contents: Read) for Facets-cloud/cur-web. See
#     scripts/server-bootstrap.sh.
#   - Current user has passwordless sudo for `install`, `rsync`, `systemctl`.
#   - Systemd unit cur-web.service installed at /etc/systemd/system/.
#   - /var/data/cur-web exists and is owned by the run user.
#   - /var/www/cur-web exists and is writable by the run user.

set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  echo "do NOT run as root — gh auth lives in your user's ~/.config/gh" >&2
  exit 1
fi

REPO="${REPO:-Facets-cloud/cur-web}"
TAG="${1:-latest}"
BIN_PATH="/usr/local/bin/cur-web"
WEB_ROOT="/var/www/cur-web"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo ">> Fetching release ${TAG} from ${REPO}"
cd "$TMP"
if [[ "$TAG" == "latest" ]]; then
  gh release download --repo "$REPO" --pattern "*.tar.gz*" --dir .
else
  gh release download "$TAG" --repo "$REPO" --pattern "*.tar.gz*" --dir .
fi

echo ">> Verifying sha256 sums"
sha256sum -c cur-web-linux-amd64.tar.gz.sha256
sha256sum -c frontend-dist.tar.gz.sha256
sha256sum -c skill.tar.gz.sha256

echo ">> Extracting"
tar xzf cur-web-linux-amd64.tar.gz       # → ./cur-web
tar xzf frontend-dist.tar.gz             # → ./dist/
tar xzf skill.tar.gz                     # → ./aws-gcp-cost-projection/

echo ">> Installing backend binary"
sudo install -m 0755 cur-web "$BIN_PATH"

echo ">> Syncing frontend to ${WEB_ROOT}"
sudo rsync -a --delete dist/ "$WEB_ROOT/"

echo ">> Syncing skill to ${HOME}/.gemini/antigravity-cli/skills/aws-gcp-cost-projection-gemini"
mkdir -p "$HOME/.gemini/antigravity-cli/skills/aws-gcp-cost-projection-gemini"
rsync -a --delete aws-gcp-cost-projection/ "$HOME/.gemini/antigravity-cli/skills/aws-gcp-cost-projection-gemini/"

echo ">> Restarting cur-web.service"
sudo systemctl restart cur-web
sudo systemctl --no-pager status cur-web | head -10

echo ">> Done."
echo "   Smoke test:  curl -sI https://gcp-estimator.facetsapp.cloud/api/auth/me"
