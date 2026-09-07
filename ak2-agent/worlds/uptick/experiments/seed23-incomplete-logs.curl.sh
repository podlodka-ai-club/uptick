#!/bin/sh
curl --include --show-error --max-time 15 --get \
  'http://81.176.229.58:8080/v2/runs/Jv63Zln7ywXRk1sXP3jsos0N/logs' \
  --data-urlencode 'from=2030-07-30T12:37:00Z' \
  --data-urlencode 'to=2030-07-30T12:38:00Z' \
  --data-urlencode 'limit=1000'
