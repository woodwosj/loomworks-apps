#!/usr/bin/env bash
# test_gate.sh <module> [module...]
# CE v19 install/upgrade gate on a throwaway DB. Tests enabled. DB dropped after.
# Usage: test_gate.sh lw_retainer_hours lw_cc_surcharge
set -u

ODOO_BIN=/home/loomworks/Desktop/ODOO.LOOMWORKS/odoo/odoo-bin
ODOO_VENV=/home/loomworks/Desktop/ODOO.LOOMWORKS/odoo-venv/bin/python3
ADDONS=/home/loomworks/Desktop/LoomworksApps/loomworks-apps
# Odoo 19 spawns the HTTP daemon whenever --test-enable is set (some tests
# need it) and binds the port at construction, so --no-http does NOT avoid
# the bind. Give each gate run its own free port instead of the default
# 8069 (usually held by a live server) so parallel gates don't clash.
pick_port() {
  local base=$((28000 + $$ % 10000)) p
  for p in $(seq "$base" $((base + 20))); do
    if python3 -c "import socket; s=socket.socket(); s.bind(('', $p)); s.close()" 2>/dev/null; then
      GATE_PORT=$p; return 0
    fi
  done
  echo "FAIL: no free http port near $base"; exit 1
}
GATE_PORT=""
pick_port
[ -x "$ODOO_BIN" ] || { echo "FAIL: odoo-bin missing at $ODOO_BIN"; exit 1; }
MODS="${*:?usage: test_gate.sh <module> [module...]}"
DB="gate_$(echo "$MODS" | tr ' ' '_' | tr -cd 'a-z_')_$(date +%s | tail -c 6)"
LOG=/tmp/test_gate_${DB}.log
FAIL=0
# Scope the test run to the modules under test. Unscoped --test-enable runs
# the full dependency tree's tests, and stock Odoo modules fail on this box
# for environment reasons (phonenumbers version, barcode libs, ormcache
# signaling) that are not the packaged modules' concern. Install/upgrade
# still run the whole dependency tree; only the test scope narrows.
TEST_TAGS=""
for m in $MODS; do TEST_TAGS="${TEST_TAGS},/$m"; done
TEST_TAGS="${TEST_TAGS#,}"

echo "=== TEST GATE: [$MODS] db=$DB ===" | tee "$LOG"

run_gate() {
  local mode="$1"; local extra="${2:-}"
  echo "--- odoo-bin $mode [$MODS] ---" | tee -a "$LOG"
  # venv python: system python3 picks up a user-site PyPDF2 3.x that
  # breaks base's own tests (DeprecatedError) and other deps; the venv is
  # the interpreter the live server runs and has the pinned versions.
  "$ODOO_VENV" "$ODOO_BIN" --addons-path="$ADDONS,/home/loomworks/Desktop/ODOO.LOOMWORKS/odoo/addons,/home/loomworks/Desktop/ODOO.LOOMWORKS/custom-addons" \
    -d "$DB" $mode "$MODS" --stop-after-init --test-enable --test-tags="$TEST_TAGS" --http-port=$GATE_PORT --log-level=warn ${extra:-} >>"$LOG" 2>&1
  local rc=$?
  if [ $rc -ne 0 ]; then
    echo "FAIL: $mode rc=$rc" | tee -a "$LOG"
    grep -E "ERROR|CRITICAL|FAILED" "$LOG" | tail -30 | tee -a "$LOG"
    FAIL=1
  else
    echo "PASS: $mode" | tee -a "$LOG"
  fi
}

run_gate -i
if [ $FAIL -eq 0 ]; then
  run_gate -u
  # second -u: upgrade idempotency (catches missing update hooks / stale xmlids)
  if [ $FAIL -eq 0 ]; then run_gate -u "--workers=0"; fi
fi

# drop throwaway DB (TCP: socket peer-auth rejects the odoo role)
PGPASSWORD=odoo dropdb -U odoo -h localhost --if-exists "$DB" 2>>"$LOG"
echo "=== RESULT: $([ $FAIL -eq 0 ] && echo PASS || echo FAIL) (log: $LOG) ===" | tee -a "$LOG"
exit $FAIL
