#!/usr/bin/env bash
# tb.sh <wp|path-to-backend-worktree> [pytest args...] — run the backend tests of a worktree with the shared venv.
# Every run takes the machine-wide test lock (exclusive), so test runs never compete for the 4 CPUs (solver tests are time-limited; under load they fail falsely).
set -uo pipefail
T="$1"; shift || true
case "$T" in /*) D="$T";; *) D="/home/user/wt/$T/gridlytics-backend";; esac
PY=/home/user/saad-tahir/gridlytics-backend/.scratch/venv/bin/python
cd "$D" || exit 2
if [ $# -eq 0 ]; then
  exec flock /tmp/gridlytics-fulltest.lock "$PY" -m pytest backend/tests -q -x -p no:cacheprovider
else
  exec flock /tmp/gridlytics-fulltest.lock "$PY" -m pytest -q -p no:cacheprovider "$@"
fi
