#!/usr/bin/env sh
# The verify harness (FR-3), POSIX parity with scripts/verify.ps1.
#
# Six stages, always in this order, because the order is load-bearing: the cheapest failure runs
# first so a syntax error never waits on a test suite. `set -e` stops at the first non-zero stage
# and the later stages never run.
#
# No environment variable beyond those documented in .env.example is required, and no API key is
# required. The harness names `fake` for itself rather than relying on a default: ticket 032
# removed the silent fallback (FR-27), so an unconfigured checkout now fails loudly and a gate
# that did not declare its own offline-ness would be red on every clean clone.
#
# Usage: ./scripts/verify.sh [--cov-fail-under N]

set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$REPO_ROOT"

COV_FAIL_UNDER=""
while [ $# -gt 0 ]; do
    case "$1" in
        --cov-fail-under)
            COV_FAIL_UNDER="$2"
            shift 2
            ;;
        --cov-fail-under=*)
            COV_FAIL_UNDER="${1#*=}"
            shift
            ;;
        *)
            echo "verify.sh: unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [ -z "$COV_FAIL_UNDER" ]; then
    if [ ! -f .spec/coverage-ratchet ]; then
        echo "verify.sh: missing coverage ratchet file: .spec/coverage-ratchet" >&2
        exit 1
    fi
    COV_FAIL_UNDER=$(tr -d ' \t\r\n' < .spec/coverage-ratchet)
fi

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
echo "verify: tree before: HEAD $HEAD_BEFORE status-sha256 $TREE_BEFORE"

# Return when the tree is as it was; exit 3 when it moved, in which case the run is void.
settle() {
    tree_state
    echo "verify: tree after:  HEAD $HEAD_NOW status-sha256 $TREE_NOW"
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
    echo "==> [$STAGE/6] $NAME"
    echo "    $*"
    # Run with `set -e` off so the stage's own exit code survives to be reported. `if ! "$@"`
    # would work for the branch but `$?` inside it is the negation's status, i.e. always 0,
    # which made a failing stage exit 0.
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

# The gate declares its own offline-ness. FR-27 sanctions exactly three ways to select `fake` -
# the flag, this variable, or a pytest fixture - and `doctor` exposes no --provider flag, so the
# variable is the one available to a script. Exported for every stage, not just doctor: the whole
# harness is the offline gate, and `tests/conftest.py` clears this per test anyway, so the suite
# stays isolated from it.
export HARNESSED_PROVIDER=fake

stage sync   uv sync --all-extras
stage lint   uv run ruff check .
stage format uv run ruff format --check .
stage types  uv run mypy src/harnessed
stage tests  uv run pytest -q -m "not live" --cov=src/harnessed "--cov-fail-under=$COV_FAIL_UNDER"
stage doctor uv run harnessed doctor --offline

settle
echo ""
echo "verify: green (6/6 stages, --cov-fail-under=$COV_FAIL_UNDER)"
exit 0
