#!/bin/sh
# Historical rejected request. A successful replay advances this simulation.
curl --include --show-error --max-time 30 -X POST http://81.176.229.58:8080/v2/runs/r3ysBAQe0Uxb3ZWZFV4JZS0q/time/advance -H 'Content-Type: application/json' --data-raw '{"request_id":"ak2-8953e8b9a3574c559a50-1101484be09841c58f9dfe3a3f35d532","duration_seconds":1800,"stop_when":{"new_log_errors":1}}'
