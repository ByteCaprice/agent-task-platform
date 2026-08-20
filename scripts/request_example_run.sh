#!/usr/bin/env bash
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8765}"
KEY="${KEY:?Set KEY to an API key with runs scope}"
REQUEST_ID="${REQUEST_ID:-example-$(date +%s)}"

curl -sS "$BASE/healthz"
curl -sS -X POST "$BASE/v1/runs" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $KEY" \
  -d "{\"route_tag\":\"example.calculator\",\"request_id\":\"$REQUEST_ID\",\"input\":{\"city\":\"Example City\",\"expression\":\"12 * (3 + 4)\"}}"
