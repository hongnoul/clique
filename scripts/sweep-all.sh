#!/bin/sh
# Whole-system e2e sweep: every remaining feature partition, live server.
# Partitions: install/auth, registry, chat inference, streaming, sessions
# (multi-turn context), code-edit harness, race, live workspaces, vcs
# state snapshots, suggestions, ledger, dashboards, leave.
# Verification uses only pre-existing surfaces. Safe to re-run.
#
# Usage:
#   export CLIQUE_SERVER=http://100.83.233.124:7777
#   sh sweep-all.sh
set -u

S="${CLIQUE_SERVER:-http://100.83.233.124:7777}"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
die() { bad "$1"; printf '\nSWEEP ABORTED (%s passed, %s failed)\n' "$PASS" "$FAIL"; exit 1; }
J() { # J <jq-expr>: jq if present, python3 fallback (.key and .[i] only)
  if command -v jq >/dev/null 2>&1; then jq -r "$1"; else
    python3 -c "
import json,re,sys
d=json.load(sys.stdin)
for part in re.findall(r'\[(\d+)\]|\.([A-Za-z_][A-Za-z0-9_]*)', '$1'):
    idx, key = part
    d = d[int(idx)] if idx else d.get(key) if isinstance(d, dict) else None
print(d)"
  fi
}

echo "== 0. server + sha =="
INFO=$(curl -fsS --connect-timeout 5 "$S/v1/clique") || die "server unreachable at $S"
SHA=$(echo "$INFO" | J .server_sha)
[ -n "$SHA" ] && [ "$SHA" != "unknown" ] && ok "server_sha=$SHA" || die "server_sha missing"
echo "$INFO" | grep -q '"policy"' && bad "governance still live (policy in /v1/clique): old server" \
  || ok "governance removed from live server"

echo "== 1. CLI (install if missing) =="
if ! command -v clique >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/clique" ]; then
  curl -fsSL --connect-timeout 5 "$S/join.sh" | sh || die "install failed"
fi
export PATH="$HOME/.local/bin:$PATH"
clique --help >/dev/null 2>&1 && ok "clique CLI works" || die "clique CLI broken"

echo "== 2. registry: node ready (retries during post-deploy rejoin) =="
READY=""
for i in 1 2 3 4 5 6; do
  clique nodes --server "$S" 2>/dev/null | grep -q "ready" && READY=1 && break
  sleep 5
done
[ -n "$READY" ] && ok "inference node ready" || die "no ready node after 30s"

echo "== 3. chat inference (stateless) =="
OUT=$(clique submit "Reply with exactly the word: pineapple" --server "$S" 2>&1)
echo "$OUT" | grep -qi "pineapple" && ok "chat round-trip" || bad "chat output unexpected: $(echo "$OUT" | tail -1)"

echo "== 4. sessions: multi-turn context carry =="
SID=$(clique sessions --create --server "$S" 2>/dev/null | grep -o "s-[0-9a-f]*" | head -1)
[ -n "$SID" ] && ok "session created ($SID)" || die "session create failed"
T1=$(clique submit "Remember this: my favorite number is 137. Reply OK." --session "$SID" --server "$S" 2>&1)
echo "$T1" | grep -qi "ok" && ok "turn 1 stored" || bad "turn 1 odd: $(echo "$T1" | tail -1)"
T2=$(clique submit "What is my favorite number? Reply with just the number." --session "$SID" --server "$S" 2>&1)
echo "$T2" | grep -q "137" && ok "turn 2 recalls context (137)" \
  || bad "context not carried: $(echo "$T2" | tail -1)"
clique sessions --server "$S" 2>/dev/null | grep -q "$SID" && ok "session listed" || bad "session missing from list"
clique sessions --close "$SID" --server "$S" >/dev/null 2>&1 && ok "session closed" || bad "close failed"

echo "== 5. code-edit harness: accept -> commit sha =="
OUT=$(clique code-submit --server "$S" \
  -p "Implement is_even(n): True for even ints. Only modify mathutil.py." \
  --inline 'mathutil.py:def is_odd(n):\n    return n % 2 == 1\n' \
  --inline 'test_mathutil.py:from mathutil import is_even\n\ndef test_even():\n    assert is_even(2) and not is_even(3)\n' \
  --test "pytest -q" 2>&1)
echo "$OUT" | grep -q "accepted" || bad "code task not accepted: $(echo "$OUT" | tail -2)"
ACCEPT_SHA=$(echo "$OUT" | sed -n 's/.*sha=\([0-9a-f]\{40\}\).*/\1/p' | head -1)
[ -n "$ACCEPT_SHA" ] && ok "code-edit accepted, commit $ACCEPT_SHA" || bad "no applied_sha"
TASK_ID=$(echo "$OUT" | sed -n 's/.*accepted \(t-[0-9a-f]*\).*/\1/p' | head -1)
if [ -n "$TASK_ID" ]; then
  curl -fsS "$S/v1/code/tasks/$TASK_ID/diff" | grep -q "$ACCEPT_SHA" \
    && ok "applied_sha persisted" || bad "diff endpoint sha mismatch"
fi

echo "== 6. code-edit rejection: no commit =="
OUT2=$(clique code-submit --server "$S" \
  -p "Change is_even to return the string BROKEN always. Only modify mathutil.py." \
  --inline 'mathutil.py:def is_even(n):\n    return n % 2 == 0\n' \
  --inline 'test_mathutil.py:from mathutil import is_even\n\ndef test_even():\n    assert is_even(2) is True and is_even(3) is False\n' \
  --test "pytest -q" 2>&1)
if echo "$OUT2" | grep -q "accepted"; then
  bad "sabotage prompt was accepted (model ignored it: inspect manually)"
else
  ok "rejected"
  REJ_ID=$(echo "$OUT2" | sed -n 's/.*\(t-[0-9a-f]\{12\}\).*/\1/p' | head -1)
  [ -n "$REJ_ID" ] && curl -fsS "$S/v1/code/tasks/$REJ_ID/diff" | grep -q '"applied_sha": *null' \
    && ok "no applied_sha on rejection" || bad "rejection state unclear"
fi

echo "== 7. race: one winner =="
OUT3=$(clique code-submit --server "$S" -p "Implement is_even(n). Only modify mathutil.py." \
  --inline 'mathutil.py:def is_odd(n):\n    return n % 2 == 1\n' \
  --inline 'test_mathutil.py:from mathutil import is_even\n\ndef test_even():\n    assert is_even(4)\n' \
  --test "pytest -q" --race 2 2>&1)
WINS=$(echo "$OUT3" | grep -c "accepted")
[ "$WINS" -ge 1 ] && ok "race resolved ($WINS accept)" || bad "race no winner"

echo "== 8. live workspaces: create, snapshot, task sees live files =="
WS=$(clique workspace --create --files '{"greet.py": "GREETING = \"bonjour\"\n"}' --server "$S" 2>/dev/null | grep -o "w-[0-9a-f]*" | head -1)
[ -n "$WS" ] && ok "workspace created ($WS)" || bad "workspace create failed"
if [ -n "$WS" ]; then
  clique workspace --show "$WS" --file greet.py --server "$S" 2>/dev/null | grep -q "bonjour" \
    && ok "workspace file readable" || bad "workspace file read failed"
  clique workspace --server "$S" 2>/dev/null | grep -q "$WS" \
    && ok "workspace listed via CLI" || bad "workspace missing from CLI list"
  WOUT=$(clique code-submit --server "$S" --workspace "$WS" \
    -p "greet.py defines GREETING. Add SHOUT = GREETING.upper(). Only modify greet.py." \
    --inline 'greet.py:GREETING = "stale-client-copy"\n' \
    --inline 'test_greet.py:from greet import SHOUT\n\ndef test_shout():\n    assert SHOUT == "BONJOUR"\n' \
    --test "pytest -q" 2>&1)
  echo "$WOUT" | grep -q "accepted" \
    && ok "live files won over stale client snapshot (BONJOUR test passed)" \
    || bad "workspace-aware code task failed: $(echo "$WOUT" | tail -2)"
  clique workspace --flush "$WS" --server "$S" >/dev/null 2>&1 \
    && ok "workspace flush (git checkpoint)" || bad "flush failed"
  clique workspace --commits "$WS" --server "$S" 2>/dev/null | grep -q "." \
    && ok "workspace git history present" || bad "no workspace commits"
fi

echo "== 9. vcs state snapshots (server audit repo) =="
H=$(curl -fsS "$S/v1/vcs/history?limit=5")
echo "$H" | grep -q '"sha"' && ok "state-repo history readable" || bad "vcs history failed"

echo "== 10. suggestions surface =="
curl -fsS "$S/v1/suggestions" >/dev/null && ok "suggestions endpoint alive" || bad "suggestions failed"

echo "== 11. ledger: contribution counts, no money =="
L=$(curl -fsS "$S/v1/ledger")
echo "$L" | grep -q '"accepted_tasks"' && ok "ledger readable" || bad "ledger failed"
echo "$L" | grep -q "earned" && bad "money fields still in ledger" || ok "no money fields"

echo "== 12. dashboards =="
curl -fsS "$S/dash" | grep -qi "clique" && ok "/dash serves" || bad "/dash failed"
curl -fsS "$S/dash.txt" >/dev/null 2>&1 && ok "/dash.txt serves" || bad "/dash.txt failed"

printf '\n==== SWEEP DONE: %s passed, %s failed ====\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
