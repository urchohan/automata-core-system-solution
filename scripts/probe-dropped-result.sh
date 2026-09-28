#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:5001}"
TIMEOUT_SECS="${TIMEOUT_SECS:-20}"

wait_for_driver_drop_pct() {
  local device_id="$1"
  local expected_drop_pct="$2"

  for _ in {1..20}; do
    if drivers="$(curl -fsS "$BASE_URL/drivers" 2>/dev/null)" &&
       printf '%s' "$drivers" |
         jq -e \
           --arg device_id "$device_id" \
           --argjson expected_drop_pct "$expected_drop_pct" \
           'any(.[]; .device_id == $device_id and (.drop_result_pct // -1) == $expected_drop_pct)' \
           >/dev/null; then
      return 0
    fi

    sleep 1
  done

  echo "FAIL: $device_id did not become ready with DROP_RESULT_PCT=$expected_drop_pct"
  return 1
}

restore() {
  echo
  echo "putting incubator-1 result delivery back to normal..."
  INC_DROP_PCT=0 docker compose up -d incubator-1 >/dev/null
}
trap restore EXIT

echo "making incubator-1 drop every result..."
INC_DROP_PCT=100 docker compose up -d incubator-1 >/dev/null
wait_for_driver_drop_pct "incubator-1" 100

before_dropped="$(curl -fsS "$BASE_URL/drivers" |
  jq -r '.[] | select(.device_id == "incubator-1") | .dropped')"

run_json="$(curl -fsS -X POST "$BASE_URL/runs" \
  -H 'Content-Type: application/json' \
  -d '{}')"
run_id="$(printf '%s' "$run_json" | jq -r '.id')"
echo "run: $run_id"

curl -fsS -X POST "$BASE_URL/runs/$run_id/start" >/dev/null

for ((i=0; i<TIMEOUT_SECS; i++)); do
  run="$(curl -fsS "$BASE_URL/runs/$run_id")"
  drivers="$(curl -fsS "$BASE_URL/drivers")"

  status="$(printf '%s' "$run" | jq -r '.run.status')"
  inc_busy="$(printf '%s' "$drivers" |
    jq -r '.[] | select(.device_id == "incubator-1") | .busy')"
  after_dropped="$(printf '%s' "$drivers" |
    jq -r '.[] | select(.device_id == "incubator-1") | .dropped')"
  dispatched_incubator_steps="$(printf '%s' "$run" |
    jq '[.steps[] | select(.device_id == "incubator-1" and .status == "dispatched")] | length')"

  if [[ "$status" == "running" \
     && "$inc_busy" == "false" \
     && "$after_dropped" -gt "$before_dropped" \
     && "$dispatched_incubator_steps" -gt 0 ]]; then
    echo
    echo "observed state"
    echo "  run status:                    $status"
    echo "  incubator busy:                $inc_busy"
    echo "  incubator dropped:             $before_dropped -> $after_dropped"
    echo "  dispatched incubator step(s):  $dispatched_incubator_steps"

    printf '%s\n' "$run" |
      jq -r '.steps[]
        | select(.device_id == "incubator-1")
        | "  \(.name): \(.status), dispatch_count=\(.dispatch_count)"'

    echo
    echo "  EXPECTED LIMITATION"
    echo "  The device finished and became idle, but the executor still has one or more"
    echo "  incubator steps as dispatched because their StepResult messages were dropped."
    exit 0
  fi

  if [[ "$status" == "completed" || "$status" == "failed" ]]; then
    echo
    echo "  UNEXPECTED OBSERVATION"
    echo "  Run reached terminal state '$status' despite incubator dropping every result."
    printf '%s\n' "$run" | jq
    exit 1
  fi

  sleep 1
done

echo
echo "  UNEXPECTED OBSERVATION"
echo "  Did not observe an idle incubator with dropped results and dispatched executor state"
echo "  within ${TIMEOUT_SECS}s."
curl -fsS "$BASE_URL/runs/$run_id" | jq
curl -fsS "$BASE_URL/drivers" |
  jq '.[] | select(.device_id == "incubator-1")'
exit 1
