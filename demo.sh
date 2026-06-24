#!/usr/bin/env bash
set -euo pipefail

CONTROL="http://localhost:8000"
PASS=0; FAIL=0

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓ PASS${NC}  $*"; ((PASS++)) || true; }
fail() { echo -e "${RED}✗ FAIL${NC}  $*"; ((FAIL++)) || true; }
info() { echo -e "${BLUE}»${NC}  $*"; }
hdr()  { echo -e "\n${YELLOW}══ $* ══${NC}"; }

hdr "STEP 0 — Start Postgres + Control Plane"
docker compose up -d postgres control
info "Waiting for control plane..."
for i in $(seq 1 30); do
  if curl -sf "$CONTROL/health" > /dev/null 2>&1; then
    ok "Control plane is healthy"; break
  fi
  [ "$i" -eq 30 ] && { fail "Control plane not ready"; exit 1; }
  sleep 3
done

hdr "STEP 1 — Create Tenant A"
RESP_A=$(curl -sf -X POST "$CONTROL/v1/tenants" \
  -H "Content-Type: application/json" \
  -d '{"name":"Acme Retail Corp"}')
TENANT_A_ID=$(echo "$RESP_A" | python3 -c "import sys,json; print(json.load(sys.stdin)['tenant_id'])")
AGENT_A_ENROLL=$(echo "$RESP_A" | python3 -c "import sys,json; print(json.load(sys.stdin)['enrollment_token'])")
info "Tenant A: $TENANT_A_ID"
ok "Tenant A created"

hdr "STEP 2 — Create Tenant B"
RESP_B=$(curl -sf -X POST "$CONTROL/v1/tenants" \
  -H "Content-Type: application/json" \
  -d '{"name":"SaaS Metrics Inc"}')
TENANT_B_ID=$(echo "$RESP_B" | python3 -c "import sys,json; print(json.load(sys.stdin)['tenant_id'])")
AGENT_B_ENROLL=$(echo "$RESP_B" | python3 -c "import sys,json; print(json.load(sys.stdin)['enrollment_token'])")
info "Tenant B: $TENANT_B_ID"
ok "Tenant B created"

hdr "STEP 3 — Define Dashboards"
DASH_A=$(curl -sf -X POST "$CONTROL/v1/dashboards" \
  -H "Content-Type: application/json" \
  -d "{\"tenant_id\":\"$TENANT_A_ID\",\"name\":\"Revenue by Region\",\"sql\":\"SELECT region, SUM(revenue) AS total_revenue FROM sales GROUP BY region ORDER BY total_revenue DESC\",\"refresh_interval\":30}")
DASH_A_ID=$(echo "$DASH_A" | python3 -c "import sys,json; print(json.load(sys.stdin)['dashboard_id'])")
info "Dashboard A: $DASH_A_ID"
ok "Dashboard A created"

DASH_B=$(curl -sf -X POST "$CONTROL/v1/dashboards" \
  -H "Content-Type: application/json" \
  -d "{\"tenant_id\":\"$TENANT_B_ID\",\"name\":\"Active MRR by Plan\",\"sql\":\"SELECT plan, COUNT(*) AS customers, SUM(mrr) AS total_mrr FROM subscriptions WHERE status='active' GROUP BY plan ORDER BY total_mrr DESC\",\"refresh_interval\":30}")
DASH_B_ID=$(echo "$DASH_B" | python3 -c "import sys,json; print(json.load(sys.stdin)['dashboard_id'])")
info "Dashboard B: $DASH_B_ID"
ok "Dashboard B created"

hdr "STEP 4 — Start Agents"
printf "AGENT_A_TOKEN=%s\nAGENT_B_TOKEN=%s\nTENANT_A_ID=%s\nTENANT_B_ID=%s\n" \
  "$AGENT_A_ENROLL" "$AGENT_B_ENROLL" "$TENANT_A_ID" "$TENANT_B_ID" > .env
docker compose up -d agent-a agent-b
ok "Agents started"

hdr "STEP 5 — Wait for Results"
info "Dashboard A ID: $DASH_A_ID"
info "Dashboard B ID: $DASH_B_ID"
info "Waiting up to 120s..."
READY=false
for i in $(seq 1 60); do
  RESP_SA=$(curl -sf "${CONTROL}/v1/dashboards/${DASH_A_ID}/data" 2>/dev/null || echo '{"status":"pending"}')
  RESP_SB=$(curl -sf "${CONTROL}/v1/dashboards/${DASH_B_ID}/data" 2>/dev/null || echo '{"status":"pending"}')
  SA=$(echo "$RESP_SA" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','pending'))" 2>/dev/null || echo "pending")
  SB=$(echo "$RESP_SB" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','pending'))" 2>/dev/null || echo "pending")
  info "  [$i/60] A:$SA  B:$SB"
  if [ "$SA" = "success" ] && [ "$SB" = "success" ]; then
    READY=true
    break
  fi
  sleep 2
done

if [ "$READY" = "false" ]; then
  info "Last raw response A: $(curl -sf ${CONTROL}/v1/dashboards/${DASH_A_ID}/data 2>/dev/null || echo 'no response')"
  info "Last raw response B: $(curl -sf ${CONTROL}/v1/dashboards/${DASH_B_ID}/data 2>/dev/null || echo 'no response')"
  fail "Results did not populate in 120s"
  exit 1
fi
ok "Both dashboards have results"

hdr "STEP 6 — Read Embedding Endpoints"
info "Dashboard A (Revenue by Region):"
curl -sf "${CONTROL}/v1/dashboards/${DASH_A_ID}/data" | python3 -m json.tool
ok "Dashboard A data retrieved"

info "Dashboard B (Active MRR by Plan):"
curl -sf "${CONTROL}/v1/dashboards/${DASH_B_ID}/data" | python3 -m json.tool
ok "Dashboard B data retrieved"

hdr "STEP 7 — TENANT ISOLATION PROOF"
info "Enrolling probe agent on a completely different tenant..."
PROBE_T=$(curl -sf -X POST "$CONTROL/v1/tenants" \
  -H "Content-Type: application/json" -d '{"name":"Isolation Probe"}')
PROBE_ENROLL=$(echo "$PROBE_T" | python3 -c "import sys,json; print(json.load(sys.stdin)['enrollment_token'])")
PROBE_A=$(curl -sf -X POST "$CONTROL/v1/agent/enroll" \
  -H "Authorization: Bearer $PROBE_ENROLL" \
  -H "Content-Type: application/json" \
  -d '{"agent_version":"0.1.0","hostname":"probe"}')
PROBE_TOKEN=$(echo "$PROBE_A" | python3 -c "import sys,json; print(json.load(sys.stdin)['agent_token'])")

STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $PROBE_TOKEN" \
  "${CONTROL}/v1/agent/jobs/next?timeout=3")
[ "$STATUS" = "204" ] \
  && ok "ISOLATION CONFIRMED — Got 204: Tenant A job invisible to different tenant" \
  || fail "ISOLATION BROKEN — expected 204, got $STATUS"

hdr "STEP 8 — FAILURE MODE: Token Revocation"
REV_T=$(curl -sf -X POST "$CONTROL/v1/tenants" \
  -H "Content-Type: application/json" -d '{"name":"Revoke Test"}')
REV_ENROLL=$(echo "$REV_T" | python3 -c "import sys,json; print(json.load(sys.stdin)['enrollment_token'])")
REV_A=$(curl -sf -X POST "$CONTROL/v1/agent/enroll" \
  -H "Authorization: Bearer $REV_ENROLL" \
  -H "Content-Type: application/json" \
  -d '{"agent_version":"0.1.0","hostname":"revoke-test"}')
REV_TOKEN=$(echo "$REV_A" | python3 -c "import sys,json; print(json.load(sys.stdin)['agent_token'])")
REV_ID=$(echo "$REV_A" | python3 -c "import sys,json; print(json.load(sys.stdin)['agent_id'])")

PRE=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "${CONTROL}/v1/agent/heartbeat" \
  -H "Authorization: Bearer $REV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status":"idle"}')
[ "$PRE" = "200" ] && ok "PRE-REVOKE  heartbeat → 200 OK" || fail "Pre-revoke got $PRE"

curl -sf -X POST "${CONTROL}/v1/admin/agents/${REV_ID}/revoke" \
  -H "X-Admin-Key: dev-admin-key" | python3 -m json.tool
ok "Agent $REV_ID revoked"

POST=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "${CONTROL}/v1/agent/heartbeat" \
  -H "Authorization: Bearer $REV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status":"idle"}')
[ "$POST" = "401" ] && ok "POST-REVOKE heartbeat → 401 Unauthorized" || fail "Expected 401, got $POST"

POLL=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $REV_TOKEN" \
  "${CONTROL}/v1/agent/jobs/next?timeout=2")
[ "$POLL" = "401" ] && ok "POST-REVOKE job poll  → 401 Unauthorized" || fail "Expected 401, got $POLL"

hdr "FINAL RESULTS"
echo -e "${GREEN}PASSED: $PASS${NC}"
if [ "$FAIL" -gt 0 ]; then
  echo -e "${RED}FAILED: $FAIL${NC}"
  exit 1
else
  echo -e "${GREEN}All checks passed.${NC}"
fi
