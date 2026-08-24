#!/usr/bin/env bash
# One-time setup on the VM: install gh CLI and authenticate it against a
# fine-grained PAT scoped to read releases of Facets-cloud/cur-web.
#
# Usage:
#   curl -sSL https://github.com/Facets-cloud/cur-web/raw/main/scripts/server-bootstrap.sh -o bootstrap.sh
#   chmod +x bootstrap.sh
#   sudo ./bootstrap.sh           # then paste token + Ctrl-D when prompted
#
# Or pipe non-interactively:
#   echo "$TOKEN" | sudo ./bootstrap.sh
#
# Token requirements (Fine-grained PAT, NOT classic):
#   - Resource owner:  Facets-cloud
#   - Repository access: Only select → cur-web
#   - Permissions:     Contents → Read-only

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "must run as root (sudo)" >&2
  exit 1
fi

REPO="Facets-cloud/cur-web"
RUN_USER="${SUDO_USER:-rohit}"

# 1. Install gh from the official Debian/Ubuntu apt repo.
if ! command -v gh >/dev/null 2>&1; then
  echo ">> Installing gh CLI"
  apt-get update -qq
  apt-get install -y -qq curl gnupg ca-certificates
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | gpg --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
  chmod 644 /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list
  apt-get update -qq
  apt-get install -y -qq gh
fi
echo ">> $(gh --version | head -1)"

# 2. Read the PAT from stdin and auth gh as the run user (NOT root).
#    Heredoc avoids leaking the token via argv / shell history.
echo ">> Paste fine-grained PAT, then Ctrl-D:"
TOKEN="$(cat)"
if [[ -z "$TOKEN" ]]; then
  echo "no token provided" >&2; exit 1
fi
sudo -u "$RUN_USER" gh auth login --with-token --hostname github.com <<<"$TOKEN"
unset TOKEN

# 3. Verify the token can read this repo's releases.
echo ">> gh auth status:"
sudo -u "$RUN_USER" gh auth status
echo ">> Probing $REPO release access:"
if sudo -u "$RUN_USER" gh release view --repo "$REPO" --json tagName -q .tagName >/dev/null; then
  echo ">> ✓ token authorized for $REPO releases"
else
  echo ">> ✗ token CANNOT read $REPO releases — check Contents:Read permission"
  exit 1
fi
