#!/usr/bin/env sh
# The dashboard's verify harness (FR-43, decision 30), POSIX parity with
# scripts/verify-dashboard.ps1.
#
# Seven stages, always in this order, cheapest failure first; the first non-zero stage stops the
# script and the later stages never run. It is its own command and never a seventh stage of
# scripts/verify.sh, which stays at six.
#
# `npm ci` and `npx playwright install chromium` fill the caches once, like `uv sync`; every stage
# after runs offline. The build stage writes dashboard/out/ (output: 'export'); the export stage
# stages it into src/harnessed/dashboard_static/ and writes the staleness stamp (FR-63,
# decision 44). The e2e stage starts its own seeded servers, never the operator's.
#
# Usage: ./scripts/verify-dashboard.sh

set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$REPO_ROOT"

# No analytics, ever (the dashboard stack table).
export NEXT_TELEMETRY_DISABLED=1

# The full harness scores the whole surface - lib/**, components/** and app/** - whether or not a
# test imported the file, against the same floor a narrow per-ticket verify uses (decision 30,
# ticket 118). Unset, the scope is whatever the run imported.
export COVERAGE_SCOPE=all

# The quiet-tree lines (FR-159, decision 71). Not stages: they print HEAD and a sha256 of the
# porcelain status before stage 1 and after the last, and a run whose tree moved between them
# proves nothing about either tree, so it is void and exits 3 whatever the stages did.
VOID_EXIT=3

tree_hash() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | cut -d' ' -f1
    else
        shasum -a 256 | cut -d' ' -f1
    fi
}

tree_state() {
    HEAD_NOW=$(git rev-parse HEAD)
    TREE_NOW=$(git status --porcelain=v1 --untracked-files=all | tree_hash)
}

tree_state
HEAD_BEFORE=$HEAD_NOW
TREE_BEFORE=$TREE_NOW
echo "verify-dashboard: tree before: HEAD $HEAD_BEFORE status-sha256 $TREE_BEFORE"

# Return when the tree is as it was; exit 3 when it moved, in which case the run is void.
settle() {
    tree_state
    echo "verify-dashboard: tree after:  HEAD $HEAD_NOW status-sha256 $TREE_NOW"
    if [ "$HEAD_NOW" != "$HEAD_BEFORE" ] || [ "$TREE_NOW" != "$TREE_BEFORE" ]; then
        echo "void: the tree changed under the run" >&2
        echo "  before: HEAD $HEAD_BEFORE status-sha256 $TREE_BEFORE" >&2
        echo "  after:  HEAD $HEAD_NOW status-sha256 $TREE_NOW" >&2
        exit "$VOID_EXIT"
    fi
}

STAGE=0

stage() {
    NAME="$1"
    shift
    STAGE=$((STAGE + 1))
    echo ""
    echo "==> [$STAGE/7] $NAME"
    echo "    $*"
    # `set -e` is off for the stage so its own exit code survives to be reported.
    set +e
    "$@"
    STATUS=$?
    set -e
    if [ "$STATUS" -ne 0 ]; then
        echo "FAILED at stage $STAGE ($NAME), exit code $STATUS" >&2
        settle
        exit "$STATUS"
    fi
}

stage install   npm --prefix dashboard ci --prefer-offline
stage lint      npm --prefix dashboard run lint
stage typecheck npm --prefix dashboard run typecheck
stage test      npm --prefix dashboard run test
stage build     npm --prefix dashboard run build
stage export    npm --prefix dashboard run export
stage e2e       npm --prefix dashboard run e2e

settle
echo ""
echo "verify-dashboard: green (7/7 stages)"
exit 0
