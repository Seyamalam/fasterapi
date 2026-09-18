#!/usr/bin/env bash
# Install the repo's git hooks (no GitHub Actions — everything runs locally).
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
git config core.hooksPath .githooks
chmod +x .githooks/pre-push .githooks/pre-commit
echo "hooks installed: $(git config core.hooksPath)"
ls -la .githooks/
