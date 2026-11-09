#!/usr/bin/env bash
# mkwt.sh <wp> [base] — make (or reuse) side-by-side worktrees for one work package:
#   /home/user/wt/<wp>/gridlytics-backend   branch audit/<wp>
#   /home/user/wt/<wp>/gridlytics-frontend  branch audit/<wp>
# base defaults to origin/fix/offgrid-carry-over (round 6, the open PR) in both repos.
# Shares the backend venv and the web app's node_modules by symlink. Never touches either main checkout's files or data/.
set -euo pipefail
WP="$1"; BASE="${2:-origin/fix/offgrid-carry-over}"
BR="audit/$WP"; ROOT="/home/user/wt/$WP"
MB=/home/user/saad-tahir/gridlytics-backend; MF=/home/user/gridlytics-frontend
mkdir -p "$ROOT"
mk() { local main="$1" dir="$2"
  if [ -d "$dir/.git" ] || [ -f "$dir/.git" ]; then echo "reuse $dir ($(git -C "$dir" rev-parse --abbrev-ref HEAD))"; return; fi
  if git -C "$main" show-ref --verify --quiet "refs/heads/$BR"; then git -C "$main" worktree add -q "$dir" "$BR"
  else git -C "$main" worktree add -q -b "$BR" "$dir" "$BASE"; fi
  echo "made $dir on $BR from $BASE"; }
mk "$MB" "$ROOT/gridlytics-backend"
mk "$MF" "$ROOT/gridlytics-frontend"
mkdir -p "$ROOT/gridlytics-backend/.scratch"
[ -e "$ROOT/gridlytics-backend/.scratch/venv" ] || ln -s "$MB/.scratch/venv" "$ROOT/gridlytics-backend/.scratch/venv"
[ -e "$ROOT/gridlytics-frontend/frontend/node_modules" ] || ln -s "$MF/frontend/node_modules" "$ROOT/gridlytics-frontend/frontend/node_modules"
grep -qxF '.scratch/' "$ROOT/gridlytics-backend/.git/info/exclude" 2>/dev/null || true
echo "backend:  $ROOT/gridlytics-backend"
echo "frontend: $ROOT/gridlytics-frontend"
