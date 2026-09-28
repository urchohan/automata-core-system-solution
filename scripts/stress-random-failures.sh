#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:5001}"
RUNS="${RUNS:-10}"
TIMEOUT_SECS="${TIMEOUT_SECS:-30}"

LH_FAIL_PCT="${LH_FAIL_PCT:-10}"
INC_FAIL_PCT="${INC_FAIL_PCT:-40}"
PR_FAIL_PCT="${PR_FAIL_PCT:-30}"

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
  echo "restoring worker failure probabilities to 0..."
  LH_FAIL_PCT=0 INC_FAIL_PCT=0 PR_FAIL_PCT=0 \
    docker compose up -d liquid-handler-1 incubator-1 plate-reader-1 >/dev/null
}
trap restore EXIT

echo "configuring random worker failures..."
echo "  liquid-handler-1: ${LH_FAIL_PCT}%"
echo "  incubator-1:      ${INC_FAIL_PCT}%"
echo "  plate-reader-1:   ${PR_FAIL_PCT}%"
echo "  runs:             ${RUNS}"

LH_FAIL_PCT="$LH_FAIL_PCT" \
INC_FAIL_PCT="$INC_FAIL_PCT" \
PR_FAIL_PCT="$PR_FAIL_PCT" \
  docker compose up -d liquid-handler-1 incubator-1 plate-reader-1 >/dev/null

wait_for_driver_fail_pct "liquid-handler-1" "$LH_FAIL_PCT"
wait_for_driver_fail_pct "incubator-1" "$INC_FAIL_PCT"
wait_for_driver_fail_pct "plate-reader-1" "$PR_FAIL_PCT"

completed=0
failed=0

for ((n=1; n<=RUNS; n++)); do
  run_json="$(curl -fsS -X POST "$BASE_URL/runs" \
    -H 'Content-Type: application/json' \
    -d '{}')"
  run_id="$(printf '%s' "$run_json" | jq -r '.id')"

  curl -fsS -X POST "$BASE_URL/runs/$run_id/start" >/dev/null

  terminal_status=""

  for ((i=0; i<TIMEOUT_SECS; i++)); do
    run="$(curl -fsS "$BASE_URL/runs/$run_id")"
    status="$(printf '%s' "$run" | jq -r '.run.status')"

    if [[ "$status" == "completed" || "$status" == "failed" ]]; then
      terminal_status="$status"
      break
    fi

    sleep 1
  done

  if [[ -z "$terminal_status" ]]; then
    echo "FAIL: run $n ($run_id) did not terminate within ${TIMEOUT_SECS}s"
    curl -fsS "$BASE_URL/runs/$run_id" | jq
    exit 1
  fi

  run="$(curl -fsS "$BASE_URL/runs/$run_id")"

  # Non-retryable liquid-handler steps may run once; retryable devices may run twice.
  bad_dispatch_count="$(printf '%s' "$run" |
    jq '[
      .steps[]
      | select(
          (.device_id == "liquid-handler-1" and .dispatch_count > 1)
          or
          ((.device_id == "incubator-1" or .device_id == "plate-reader-1") and .dispatch_count > 2)
        )
    ] | length')"

  if [[ "$bad_dispatch_count" -ne 0 ]]; then
    echo "FAIL: run $n ($run_id) exceeded the allowed dispatch count"
    printf '%s\n' "$run" |
      jq -r '.steps[] | "  \(.name): \(.status), device=\(.device_id), dispatch_count=\(.dispatch_count)"'
    exit 1
  fi

  # A completed step must never have an incomplete dependency.
  dependency_violation="$(printf '%s' "$run" |
    jq '
      (.steps | map({key: .name, value: .status}) | from_entries) as $status_by_name
      | [
          .steps[]
          | select(.status == "completed")
          | select(any(.depends_on[]?; $status_by_name[.] != "completed"))
        ]
      | length
    ')"

  if [[ "$dependency_violation" -ne 0 ]]; then
    echo "FAIL: run $n ($run_id) completed a step before all dependencies completed"
    printf '%s\n' "$run" | jq
    exit 1
  fi

  if [[ "$terminal_status" == "completed" ]]; then
    ((completed += 1))
  else
    ((failed += 1))
  fi

  echo "  run $n/$RUNS: $terminal_status ($run_id)"
done

echo
echo "PASS  all ${RUNS} random-failure runs reached a valid terminal state"
echo "  completed: $completed"
echo "  failed:    $failed"
echo "  no dependency violations observed"
echo "  no step exceeded its allowed dispatch count"
