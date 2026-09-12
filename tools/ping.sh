#!/bin/sh
# Ask GitHub to run the build. Run this every ten minutes from cron, e.g. on the Unraid box:
#   7,17,27,37,47,57 * * * *  GITHUB_TOKEN=... /path/to/ping.sh
# The build itself only deploys when something changed, so pinging is cheap.
# Token: a fine-grained personal access token for this repository with "Actions: read and write".
set -eu
: "${GITHUB_TOKEN:?set GITHUB_TOKEN to a fine-grained token with Actions read/write on the repo}"
curl -sS -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${REPO:-Gongreg/group-im}/actions/workflows/deploy.yml/dispatches" \
  -d '{"ref":"main"}'
