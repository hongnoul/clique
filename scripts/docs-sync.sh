#!/bin/sh
# clique docs-sync: apply a workspace export bundle to the GitHub repo docs.
#
# The dogfood loop drafts docs in the internal socket VCS (live workspace).
# This script is the bridge back to GitHub: it takes an export bundle and
# writes the files into a repo checkout, leaving commit + PR to the human
# (or to `gh` on a credentialed box).
#
# Step 1 (any machine, no creds): export the bundle from the clique.
#   clique workspace --export <workspace-id> --file <path> > /tmp/bundle.txt
#   # or full JSON: curl -s $CLIQUE_SERVER/v1/workspaces/<id>/export
#
# Step 2 (credentialed checkout): apply it.
#   sh scripts/docs-sync.sh /tmp/bundle.json [--checkout ~/git/tcj] [--apply]
#
# Default is --dry-run: prints what would change. --apply writes files.
# Commit + push + PR always stay manual (review gate for agent drafts).
set -eu

BUNDLE="${1:-}"
CHECKOUT="${2:-}"
APPLY=0
for a in "$@"; do
    case "$a" in
        --apply) APPLY=1 ;;
        --checkout) shift ;;
    esac
done
# parse --checkout <dir> properly
CHECKOUT_DIR="$HOME/git/tcj"
prev=""
for a in "$@"; do
    if [ "$prev" = "--checkout" ]; then CHECKOUT_DIR="$a"; fi
    prev="$a"
done

if [ -z "$BUNDLE" ] || [ ! -f "$BUNDLE" ]; then
    echo "usage: sh scripts/docs-sync.sh <bundle.json> [--checkout DIR] [--apply]" >&2
    echo "  bundle: JSON from GET /v1/workspaces/<id>/export" >&2
    exit 2
fi
if [ ! -d "$CHECKOUT_DIR/.git" ]; then
    echo "error: $CHECKOUT_DIR is not a git checkout" >&2
    exit 1
fi

python3 - "$BUNDLE" "$CHECKOUT_DIR" "$APPLY" <<'EOF'
import json, sys
from pathlib import Path

bundle_path, checkout, apply = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
bundle = json.loads(Path(bundle_path).read_text())
files = bundle.get("files", {})
print(f"workspace {bundle.get('workspace_id')} seq={bundle.get('seq')} "
      f"files={sorted(files)}")
for sha in (bundle.get("commits", []) or [])[:5]:
    print(f"  provenance {sha.get('sha', '')[:10]} {sha.get('message', '')}")
root = Path(checkout)
for path, text in files.items():
    if ".." in Path(path).parts or path.startswith("/"):
        print(f"SKIP forbidden path: {path}")
        continue
    dest = root / path
    old = dest.read_text() if dest.is_file() else None
    if old == text:
        print(f"unchanged: {path}")
        continue
    print(f"{'WRITE' if apply else 'would write'}: {path} "
          f"({len(old or '')} -> {len(text)} chars)")
    if apply:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
if not apply:
    print("dry run: re-run with --apply to write, then review + commit + PR.")
EOF
