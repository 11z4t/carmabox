#!/bin/bash
# PLAT-1212: Install git hooks for CARMA Box development.
#
# Sets core.hooksPath to .githooks/ so pre-commit and commit-msg
# hooks are automatically applied to every commit.
#
# Run once per clone:
#   bash scripts/install-hooks.sh

set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOKS_DIR="$REPO_ROOT/.githooks"

if [ ! -d "$HOOKS_DIR" ]; then
    echo "ERROR: $HOOKS_DIR not found. Are you in the carmabox repo?" >&2
    exit 1
fi

git config core.hooksPath .githooks
echo "Git hooks installed: core.hooksPath = .githooks"

# Verify hooks are executable
for hook in "$HOOKS_DIR"/*; do
    if [ -f "$hook" ] && [ ! -x "$hook" ]; then
        chmod +x "$hook"
        echo "Made executable: $(basename "$hook")"
    fi
done

echo ""
echo "Active hooks:"
for hook in "$HOOKS_DIR"/*; do
    [ -f "$hook" ] && echo "  $(basename "$hook")"
done
echo ""
echo "Done. Hooks will run on every 'git commit'."
