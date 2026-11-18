#!/bin/bash
# Supervisor: retry the GitHub→Qoder→PAT→Pro-trial pipeline until 1 success.
cd /forge/Projects/github/github-regkit-mibp
PY=.venv/bin/python
PATS=accounts/qoder_pats.txt

for i in 1 2 3 4 5 6; do
  echo "══════════ ATTEMPT $i/6 ══════════"
  pats_before=$(grep -c "pt-" "$PATS" 2>/dev/null || echo 0)

  echo "--- stage 1: mint GitHub account ---"
  $PY main.py --count 1 || true

  echo "--- stage 2: qoder via GitHub OAuth ---"
  $PY -m github_register.qoder_flow --via-github --count 1 --engine patchright || true

  pats_after=$(grep -c "pt-" "$PATS" 2>/dev/null || echo 0)
  if [ "$pats_after" -gt "$pats_before" ]; then
    echo "--- stage 3: inject PAT → claim Pro trial credits ---"
    $PY -m github_register.qoder_inject --pats "$PATS" || true
    last=$(grep "pro_claimed" accounts/qoder_inject_results.jsonl 2>/dev/null | tail -1)
    if echo "$last" | grep -q '"pro_claimed": true'; then
      echo "══════════ SUCCESS: PRO TRIAL ACTIVE ══════════"
      echo "$last"
      exit 0
    fi
  else
    echo "[SUPERVISOR] no new PAT this attempt"
  fi
  echo "[SUPERVISOR] attempt $i failed — waiting 20s"
  sleep 20
done
echo "══════════ ALL 6 ATTEMPTS EXHAUSTED ══════════"
exit 1
