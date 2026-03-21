#!/bin/bash
# Mock `gh` CLI for integration tests.
#
# Place the directory containing this script at the front of PATH:
#   export PATH="$SPREAD_PATH/tests/spread/lib/mock-gh-bin:$PATH"
#
# The mock responds to the `gh` subcommands that update_issue.py uses:
#   gh issue view <number> --json body
#   gh issue view <number> --json assignees
#   gh issue view <number> --json comments
#   gh issue edit <number> ...
#   gh issue comment <number> ...
#
# Behaviour is controlled by environment variables:
#   MOCK_GH_ISSUE_BODY — the Markdown body returned by `gh issue view --json body`
#   MOCK_GH_COMMENTS    — JSON array of comments (default: [])
#   MOCK_GH_LOG         — file to append commands to (for assertions)

set -eu

log() {
  if [ -n "${MOCK_GH_LOG:-}" ]; then
    echo "$*" >> "$MOCK_GH_LOG"
  fi
}

log "gh $*"

# Parse the subcommand chain.
if [ "${1:-}" = "issue" ]; then
  shift
  case "${1:-}" in
    view)
      shift
      ISSUE_NUMBER="${1:-}"
      shift
      # Determine what JSON field is requested.
      if [ "${1:-}" = "--json" ]; then
        FIELD="${2:-}"
        case "$FIELD" in
          body)
            echo "{\"body\": $(echo "${MOCK_GH_ISSUE_BODY:-No body}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}"
            ;;
          assignees)
            echo '{"assignees": []}'
            ;;
          comments)
            echo "{\"comments\": ${MOCK_GH_COMMENTS:-[]}}"
            ;;
          *)
            echo "{}"
            ;;
        esac
      fi
      ;;
    edit)
      # Just log and succeed.
      ;;
    comment)
      # Just log and succeed.
      ;;
    *)
      echo "mock-gh: unknown issue subcommand: $1" >&2
      exit 1
      ;;
  esac
else
  echo "mock-gh: unknown command: $1" >&2
  exit 1
fi
