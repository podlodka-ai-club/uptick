#!/bin/sh
# The agent observed HTTP 200 followed by an incomplete response body.
# Recorded failures are in seed42-incomplete-logs.json.
curl --include --show-error --max-time 15 --get \
  'http://81.176.229.58:8080/v2/runs/8t8d1my1o5QXFrtBjoNPud8a/logs' \
  --data-urlencode 'from=2030-01-15T15:24:40Z' \
  --data-urlencode 'to=2030-01-15T15:25:44Z' \
  --data-urlencode 'limit=200'
