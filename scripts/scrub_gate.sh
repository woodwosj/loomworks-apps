#!/usr/bin/env bash
# scrub_gate.sh <module_dir> [--strict]
# Gate: zero client-identifier hits, manifest hygiene, marketing-asset presence.
# Exit 0 = PASS, 1 = FAIL. Report written to <module_dir>/../scrub_report_<name>.txt
set -u

MODULE_DIR="${1:?usage: scrub_gate.sh <module_dir> [--strict]}"
STRICT="${2:-}"
NAME="$(basename "$MODULE_DIR")"
REPORT="$(dirname "$MODULE_DIR")/scrub_report_${NAME}.txt"
FAIL=0

{
  echo "=== SCRUB GATE: $NAME ==="
  echo "dir: $MODULE_DIR"
  echo "time: $(date -Is)"

  echo; echo "--- 1. forbidden identifier sweep (must be ZERO hits) ---"
  # NOTE: python re.I / grep -i are UNRELIABLE in this environment (uppercase not
  # matched). Lowercase the HAYSTACK instead — case handling is then environment-proof.
  FORBIDDEN_HITS=$(python3 - "$MODULE_DIR" <<'PY'
import os, re, sys
PAT = re.compile(r'(^|[^a-z0-9])(lumin|tdmotion|edc|hobesound|keva|gmsv|christian|woodworth)')
EXT = ('.py','.xml','.csv','.js','.po','.html','.md','.txt','.rst')
hits = []
for root, _, files in os.walk(sys.argv[1]):
    for f in files:
        if not f.lower().endswith(EXT):
            continue
        p = os.path.join(root, f)
        try:
            text = open(p, encoding='utf-8', errors='replace').read().lower()
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if PAT.search(line):
                hits.append(f"{p}:{i}: {line.strip()[:100]}")
if hits:
    print('\n'.join(hits[:40]))
    sys.exit(1)
print("PASS: zero identifier hits")
PY
)
  [ $? -ne 0 ] && { echo "FAIL: identifier hits:"; echo "$FORBIDDEN_HITS"; FAIL=1; }

  echo; echo "--- 2. ChatGPT author pattern (must be ZERO) ---"
  CHAT=$(python3 - "$MODULE_DIR" <<'PY'
import os, sys
hits = []
for root, _, files in os.walk(sys.argv[1]):
    for f in files:
        p = os.path.join(root, f)
        try:
            text = open(p, encoding='utf-8', errors='replace').read().lower()
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if 'chatgpt' in line:
                hits.append(f"{p}:{i}: {line.strip()[:100]}")
if hits:
    print('\n'.join(hits[:10]))
    sys.exit(1)
print("PASS")
PY
)
  [ $? -ne 0 ] && { echo "FAIL: ChatGPT hits:"; echo "$CHAT"; FAIL=1; }

  echo; echo "--- 3. hardcoded emails / secrets (report-only, review each) ---"
  grep -rnE "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}" \
    --include='*.py' --include='*.xml' --include='*.csv' "$MODULE_DIR" 2>/dev/null | head -30
  grep -rniE "api[_-]?key|secret|token[[:space:]]*=|password[[:space:]]*=" \
    --include='*.py' --include='*.xml' --include='*.csv' "$MODULE_DIR" 2>/dev/null | head -20

  echo; echo "--- 4. manifest hygiene ---"
  MANIFEST="$MODULE_DIR/__manifest__.py"
  if [ ! -f "$MANIFEST" ]; then echo "FAIL: no __manifest__.py"; FAIL=1; exit $FAIL; fi
  python3 - "$MANIFEST" <<'PY'
import ast, sys
path = sys.argv[1]
try:
    tree = ast.parse(open(path).read())
    # Odoo 19 loads manifests via ast.literal_eval ONLY (odoo/modules/module.py,
    # Manifest._from_path) -> the standard form is a bare dict literal.
    if len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr) \
            and isinstance(tree.body[0].value, ast.Dict):
        m = ast.literal_eval(tree.body[0].value)
    else:
        m = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
             if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.value
             and (isinstance(n.value, (ast.Constant, ast.List, ast.Dict, ast.Str)))}
except Exception as e:
    print(f"FAIL: manifest parse error: {e}"); sys.exit(1)
author = m.get('author', 'MISSING')
lic = m.get('license', 'MISSING')
ver = m.get('version', 'MISSING')
deps = m.get('depends', [])
bad_deps = [d for d in deps if any(x in d.lower() for x in ('lumin','edc','tdmotion','hobesound'))]
print(f"author: {author}  license: {lic}  version: {ver}")
print(f"deps: {deps}")
if author != 'Loomworks Solutions LLC':
    print(f"FAIL: author='{author}' (want 'Loomworks Solutions LLC')"); sys.exit(1)
if lic not in ('OPL-1', 'LGPL-3'):
    print(f"FAIL: license='{lic}' (want OPL-1 or LGPL-3)"); sys.exit(1)
if bad_deps:
    print(f"FAIL: client deps: {bad_deps}"); sys.exit(1)
print("PASS: manifest hygiene")
PY
  [ $? -ne 0 ] && FAIL=1

  echo; echo "--- 5. marketing assets ---"
  ICON="$MODULE_DIR/static/description/icon.png"
  HTML="$MODULE_DIR/static/description/index.html"
  [ -f "$ICON" ] && echo "PASS: icon.png" || { echo "FAIL: missing $ICON"; FAIL=1; }
  [ -f "$HTML" ] && echo "PASS: index.html" || { echo "FAIL: missing $HTML"; FAIL=1; }

  echo; echo "=== RESULT: $([ $FAIL -eq 0 ] && echo PASS || echo FAIL) ==="
  exit $FAIL  # propagate failure out of the tee subshell (see PIPESTATUS below)
} | tee "$REPORT"
PIPE_STATUS=${PIPESTATUS[0]}

if [ "$STRICT" = "--strict" ] && grep -qE "api[_-]?key|secret|token[[:space:]]*=" "$REPORT" 2>/dev/null; then
  echo "STRICT: secret-pattern hits present, failing"; exit 1
fi
exit $PIPE_STATUS
