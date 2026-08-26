#!/usr/bin/env sh
# Parse-check the dashboard's ES modules. Node is only needed for this check —
# the dashboard itself ships no build step and no node_modules.
#
#   sh scripts/check_web.sh
set -e
cd "$(dirname "$0")/.."

command -v node >/dev/null || { echo "node not found — skipping syntax check"; exit 0; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
status=0

for file in app/web/*.js app/web/views/*.js; do
  copy="$tmp/$(basename "$file" .js).mjs"
  cp "$file" "$copy"
  if ! node --check "$copy" 2>"$tmp/err"; then
    status=1
    echo "FAIL $file"
    sed "s|$copy|$file|" "$tmp/err" | head -5
  fi
done

[ "$status" -eq 0 ] && echo "app/web: all modules parse"
exit "$status"
