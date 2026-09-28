#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:5001}"
TIMEOUT_SECS="${TIMEOUT_SECS:-20}"

wait_for_driver_fail_pct() {
  local device_id="$1"
  local expected_fail_pct="$2"

  for _ in {1..20}; do
    if drivers="$(curl -fsS "$BASE_URL/drivers" 2>/dev/null)" &&
       printf '%s' "$drivers" |
         jq -e \
           --arg device_id "$device_id" \
           --argjson expected_fail_pct "$expected_fail_pct" \
           'any(.[]; .device_id == $device_id and (.fail_pct // -1) == $expected_fail_pct)' \
           >/dev/null; then
      return 0
    fi

    sleep 1
  done

  echo "FAIL: $device_id did not become ready with FAIL_PCT=$expected_fail_pct"
  return 1
}

restore() {
  echo
  echo "putting liquid-handler-1 back to normal..."
  LH_FAIL_PCT=0 docker compose up -d liquid-handler-1 >/dev/null
}
trap restore EXIT

echo "making liquid-handler-1 fail every step..."
LH_FAIL_PCT=100 docker compose up -d liquid-handler-1 >/dev/null
wait_for_driver_fail_pct "liquid-handler-1" 100

run_json="$(curl -fsS -X POST "$BASE_URL/runs" \
  -H 'Content-Type: application/json' \
  -d '{}')"
run_id="$(printf '%s' "$run_json" | jq -r '.id')"
echo "run: $run_id"

curl -fsS -X POST "$BASE_URL/runs/$run_id/start" >/dev/null

for ((i=0; i<TIMEOUT_SECS; i++)); do
  run="$(curl -fsS "$BASE_URL/runs/$run_id")"
  status="$(printf '%s' "$run" | jq -r '.run.status')"

  if [[ "$status" == "failed" ]]; then
    echo
    echo "  PASS  run ended as 'failed' after ${i}s"

    failed_steps="$(printf '%s' "$run" |
      jq '[.steps[] | select(.status == "failed")] | length')"

    retried_nonretryable="$(printf '%s' "$run" |
      jq '[.steps[] | select(.device_id == "liquid-handler-1" and .dispatch_count > 1)] | length')"

    if [[ "$failed_steps" -lt 1 ]]; then
      echo "  FAIL  no failed step recorded"
      exit 1
    fi

    if [[ "$retried_nonretryable" -ne 0 ]]; then
      echo "  FAIL  a non-retryable liquid-handler step was dispatched more than once"
      printf '%s\n' "$run" |
        jq -r '.steps[]
          | select(.device_id == "liquid-handler-1")
          | "  \(.name): \(.status), dispatch_count=\(.dispatch_count)"'
      exit 1
    fi

    printf '%s\n' "$run" |
      jq -r '.steps[]
        | "  \(.name): \(.status), dispatch_count=\(.dispatch_count)"
          + (if .error then "  (" + .error + ")" else "" end)'

    exit 0
  fi

  if [[ "$status" == "completed" ]]; then
    echo
    echo "  FAIL  run completed even though liquid-handler-1 was configured to fail every step"
    exit 1
  fi

  sleep 1
done

echo
echo "  FAIL  run did not reach a terminal state within ${TIMEOUT_SECS}s"
curl -fsS "$BASE_URL/runs/$run_id" | jq
exit 1
