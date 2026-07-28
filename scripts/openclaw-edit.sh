#!/bin/bash
# =============================================================================
# openclaw-edit.sh — read and edit openclaw/ without it ever hitting this disk
# =============================================================================
# openclaw/ is tracked and pushed like any other directory, but it is excluded
# from this Mac's working tree by sparse-checkout: Cisco Secure Endpoint (AMP)
# deletes OpenClaw artifacts from the host, and a file that is never checked
# out is a file AMP cannot delete. See .claude/napkin.md.
#
# So there is nothing to open in an editor. This script round-trips a file
# through git object storage instead: extract from HEAD into a temp dir, edit
# there, write the result straight into the index. The repo path stays empty
# the whole time.
#
#   openclaw-edit.sh --list                       what is in openclaw/
#   openclaw-edit.sh --show  <path>               print a file
#   openclaw-edit.sh         <path>               edit and stage it ($EDITOR)
#   openclaw-edit.sh --apply-sparse               (re)apply the sparse config
#
# <path> is repo-relative, e.g. openclaw/plugins/demobot-toolguard/index.js.
# Edits land STAGED; commit them. run-openclaw.sh builds the image from
# HEAD:openclaw, so a change reaches the gateway once committed, not once staged.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

# The one definition of "everything except openclaw/". .githooks/post-checkout
# calls back into --apply-sparse so new worktrees inherit exactly this.
SPARSE_PATTERNS=('/*' '!/openclaw/')

usage() { sed -n '5,22p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

apply_sparse() {
  git sparse-checkout set --no-cone "${SPARSE_PATTERNS[@]}" 2>/dev/null && return 0
  # Porcelain fallback: --no-cone is the less-travelled path in modern git.
  git config core.sparseCheckout true
  printf '%s\n' "${SPARSE_PATTERNS[@]}" > "$(git rev-parse --git-path info/sparse-checkout)"
  git read-tree -mu HEAD
}

check_path() {
  case "$1" in
    openclaw/*) ;;
    *) echo "ERROR: $1 is not under openclaw/ — edit it normally, it is checked out." >&2; exit 2 ;;
  esac
}

case "${1:---help}" in
  -h|--help) usage 0 ;;
  --apply-sparse) apply_sparse; echo "sparse-checkout applied: openclaw/ excluded from the working tree"; exit 0 ;;
  --list) git ls-tree -r --name-only HEAD openclaw/; exit 0 ;;
  --show)
    [ $# -eq 2 ] || usage 2
    check_path "$2"
    git show "HEAD:$2"; exit 0 ;;
  -*) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

[ $# -eq 1 ] || usage 2
PATH_IN_REPO="$1"
check_path "$PATH_IN_REPO"

TMPDIR_=$(mktemp -d)
trap 'rm -rf "$TMPDIR_"' EXIT
# Keep the basename so $EDITOR still picks the right syntax highlighting; only
# the directory differs from the repo path.
TMP="$TMPDIR_/$(basename "$PATH_IN_REPO")"

if git cat-file -e "HEAD:$PATH_IN_REPO" 2>/dev/null; then
  git show "HEAD:$PATH_IN_REPO" > "$TMP"
  MODE=$(git ls-tree HEAD "$PATH_IN_REPO" | awk '{print $1}')
else
  echo "NOTE: $PATH_IN_REPO is not in HEAD — creating it."
  : > "$TMP"
  MODE=100644
fi
BEFORE=$(git hash-object "$TMP")

"${EDITOR:-vi}" "$TMP"

BLOB=$(git hash-object -w "$TMP")
if [ "$BLOB" = "$BEFORE" ]; then
  echo "No change to $PATH_IN_REPO — nothing staged."
  exit 0
fi

git update-index --add --cacheinfo "$MODE,$BLOB,$PATH_IN_REPO"
# --cacheinfo rewrites the index entry, which drops the skip-worktree bit that
# sparse-checkout relies on. Put it back, or the next checkout materialises the
# file and hands AMP a target.
if [ "$(git config --get core.sparseCheckout || true)" = "true" ]; then
  git update-index --skip-worktree "$PATH_IN_REPO"
fi

echo "Staged $PATH_IN_REPO ($BLOB). Nothing was written to the working tree."
echo "Commit it — run-openclaw.sh builds from HEAD:openclaw, so a staged-only"
echo "edit will not reach the gateway image."
