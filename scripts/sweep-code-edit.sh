#!/bin/sh
# Code-edit harness + code-repo VCS e2e sweep.
# Run on any machine. Safe to re-run. Exits nonzero on first failure.
#
# Prereqs: curl. jq optional (falls back to python3). `clique` CLI optional
#   (installs via join.sh if missing).
#
# Usage:
#   export CLIQUE_SERVER=http://100.83.233.124:7777   # or the public_url
#   sh sweep-code-edit.sh
set -eu

S="${CLIQUE_SERVER:-http://100.83.233.124:7777}"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
die() { bad "$1"; printf '\nSWEEP ABORTED (%s passed, %s failed)\n' "$PASS" "$FAIL"; exit 1; }
J() { # tiny jq replacement: J <expr>  (reads stdin)
  if command -v jq >/dev/null 2>&1; then jq -r "$1"; else
    python3 -c "import json,sys
d=json.load(sys.stdin)
for part in '$1'.strip('.').split('.'):
    if part: d=d[int(part)] if part.lstrip('-').isdigit() and isinstance(d,list) else d.get(part) if isinstance(d,dict) else d
print(d)"
  fi
}

echo "== 0. server reachable + on expected sha =="
INFO=$(curl -fsS --connect-timeout 5 "$S/v1/clique") || die "server unreachable at $S"
SHA=$(echo "$INFO" | J .server_sha)
echo "  server_sha=$SHA"
case "$SHA" in de389a8*|*[!u]*) ;; esac
[ "$SHA" != "unknown" ] && [ -n "$SHA" ] && ok "server sha reported ($SHA)" || die "server_sha missing"

echo "== 1. CLI present (install if missing) =="
if ! command -v clique >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/clique" ]; then
  echo "  installing via join.sh..."
  curl -fsSL --connect-timeout 5 "$S/join.sh" | sh || die "install failed"
fi
export PATH="$HOME/.local/bin:$PATH"
clique --help >/dev/null 2>&1 && ok "clique CLI works" || die "clique CLI broken"

echo "== 2. inference node ready =="
clique nodes --server "$S" | grep -q "ready" && ok "at least one node ready" \
  || die "no ready node (is gx10-vllm up?)"

echo "== 3. happy path: code-submit -> accept -> commit sha =="
OUT=$(clique code-submit --server "$S" \
  -p "Implement is_even(n) in mathutil.py: return True when n is an even integer, False otherwise. Keep is_odd unchanged. Only modify mathutil.py." \
  --inline 'mathutil.py:def is_odd(n):\n    return n % 2 == 1\n' \
  --inline 'test_mathutil.py:from mathutil import is_odd, is_even\n\ndef test_even():\n    assert is_even(2)\n    assert not is_even(3)\n\ndef test_odd_untouched():\n    assert is_odd(3)\n' \
  --test "pytest -q" 2>&1) || true
echo "$OUT" | tail -5
echo "$OUT" | grep -q "accepted" || die "code task not accepted"
ACCEPT_SHA=$(echo "$OUT" | sed -n 's/.*sha=\([0-9a-f]\{40\}\).*/\1/p' | head -1)
[ -n "$ACCEPT_SHA" ] && ok "accepted with commit sha $ACCEPT_SHA" || die "no applied_sha in output"
echo "$OUT" | grep -q "mathutil.py" && ok "diff shown, touches mathutil.py" || bad "diff not printed"

echo "== 4. code-repo audit: history + nested-tree diff (regression de389a8) =="
CHIST=$(curl -fsS "$S/v1/vcs/history?repo=code") || die "/v1/vcs/history?repo=code failed"
echo "$CHIST" | grep -q "code task" && ok "code-repo history has accepted commit" \
  || bad "no code-task commit in code-repo history"
CHEAD=$(echo "$CHIST" | J '.0.sha')
CPARENT=$(echo "$CHIST" | J '.1.sha')
if [ -n "$CPARENT" ] && [ "$CPARENT" != "None" ]; then
  DIFF=$(curl -fsS "$S/v1/vcs/diff?repo=code&a=$CPARENT&b=$CHEAD") \
    || die "code-repo diff 500: nested-tree regression (pre-de389a8 server?)"
  echo "$DIFF" | grep -q "mathutil.py" \
    && ok "nested-tree diff shows <task_id>/mathutil.py" \
    || bad "diff missing expected nested path"
fi

echo "== 5. rejection path: failing tests leave no commit =="
BEFORE=$(curl -fsS "$S/v1/vcs/history?repo=code" | J '.0.sha')
OUT2=$(clique code-submit --server "$S" \
  -p "Change is_even to always return the string BROKEN regardless of input. Only modify mathutil.py." \
  --inline 'mathutil.py:def is_even(n):\n    return n % 2 == 0\n' \
  --inline 'test_mathutil.py:from mathutil import is_even\n\ndef test_even():\n    assert is_even(2) is True\n    assert is_even(3) is False\n' \
  --test "pytest -q" 2>&1) || true
echo "$OUT2" | tail -3
if echo "$OUT2" | grep -q "accepted"; then
  bad "harness accepted a patch that should fail tests (model may have ignored the sabotage prompt: inspect manually)"
else
  ok "rejected (tests failed or diff invalid)"
fi
AFTER=$(curl -fsS "$S/v1/vcs/history?repo=code" | J '.0.sha')
[ "$BEFORE" = "$AFTER" ] && ok "no new commit on rejection" || bad "rejection created a commit"

echo "== 6. race: one winner, siblings cancelled =="
OUT3=$(clique code-submit --server "$S" \
  -p "Implement is_even(n) returning True for even ints. Only modify mathutil.py." \
  --inline 'mathutil.py:def is_odd(n):\n    return n % 2 == 1\n' \
  --inline 'test_mathutil.py:from mathutil import is_even\n\ndef test_even():\n    assert is_even(4) and not is_even(5)\n' \
  --test "pytest -q" --race 2 2>&1) || true
echo "$OUT3" | tail -4
WINS=$(echo "$OUT3" | grep -c "accepted" || true)
[ "$WINS" -ge 1 ] && ok "race produced $WINS accept(s)" || bad "race produced no winner"

echo "== 7. ledger accrued =="
clique ledger --server "$S" 2>&1 | tail -3
clique stats --server "$S" >/dev/null 2>&1 && ok "stats endpoint alive" || bad "stats failed"

printf '\n==== SWEEP DONE: %s passed, %s failed ====\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
