#!/bin/bash
# Create a minimal charm git repository for integration testing.
#
# Usage:
#   create-test-charm.sh <target-dir> [passing|failing]
#
# "passing" (default) copies a charm that should pass all automated checks.
# "failing" copies one that intentionally fails several checks.

set -eu

TARGET="${1:?Usage: create-test-charm.sh <target-dir> [passing|failing]}"
VARIANT="${2:-passing}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FIXTURE_DIR="$SCRIPT_DIR/charms/$VARIANT"

if [ ! -d "$FIXTURE_DIR" ]; then
  echo "Unknown variant: $VARIANT (expected 'passing' or 'failing')" >&2
  exit 1
fi

mkdir -p "$TARGET"
cp -R "$FIXTURE_DIR/." "$TARGET/"

cd "$TARGET"
git init --initial-branch=main --quiet
git config user.email "test@test.local"
git config user.name "Test"
git add -A
git commit -m "Initial commit" --quiet
