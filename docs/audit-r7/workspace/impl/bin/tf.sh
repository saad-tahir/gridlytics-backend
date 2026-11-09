#!/usr/bin/env bash
# tf.sh <wp|path-to-frontend-worktree> [vitest file filters...] — typecheck + vitest of a web-app worktree.
#   no filters: the full gates (typecheck, all tests, build) under the machine-wide test lock
#   filters:    typecheck + only the named test files (shared lock)
set -uo pipefail
T="$1"; shift || true
case "$T" in /*) D="$T";; *) D="/home/user/wt/$T/gridlytics-frontend";; esac
cd "$D/frontend" || exit 2
if [ $# -eq 0 ]; then
  exec flock /tmp/gridlytics-fulltest.lock npm run -s gates
else
  exec flock /tmp/gridlytics-fulltest.lock sh -c 'npm run -s typecheck && npx vitest run "$@"' sh "$@"
fi
