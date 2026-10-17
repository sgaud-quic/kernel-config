#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# Verify that a regenerated qcom-next tree is the tree that CI tested.
#
# Publication is a cross-repository operation: qcom-next is force-pushed and the
# kernel-config PR is merged. Those cannot be made atomic, so before publishing
# we regenerate the tree from the exact PR config and prove it carries the same
# inputs that passed the gates. This script is that proof.
#
# Two modes:
#
#   compare two tested-set records
#     check_tested_tree_matches.sh --expected tested-set.txt --actual regen.txt
#
#   verify a git tree really contains a tested set
#     check_tested_tree_matches.sh --expected tested-set.txt \
#                                  --kernel-dir DIR [--ref qcom-next]
#
# Both modes can be combined. The digest covers inputs only, deliberately not
# the integration head: ci-merge stamps merge commits with the current time, so
# two runs from identical inputs produce different commit ids while consuming
# exactly the same content.
#
# Exit status:
#   0  the trees match
#   1  mismatch
#   2  usage error or missing prerequisite
set -u
set -o pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SELF_DIR/qcom_next_config.py"

EXPECTED=""
ACTUAL=""
KERNEL_DIR=""
REF="qcom-next"
QUIET=0

die()  { printf 'error: %s\n' "$*" >&2; exit 2; }
info() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }
step() { [ "$QUIET" -eq 1 ] || printf '\n==> %s\n' "$*"; }

usage() {
    cat <<'EOF'
Verify that a regenerated qcom-next tree matches the tested one.

Options:
  --expected PATH     tested-set recorded when the gates passed (required)
  --actual PATH       tested-set produced by the regeneration
  --kernel-dir DIR    kernel worktree to verify against
  --ref REF           ref to verify in --kernel-dir (default: qcom-next)
  -q, --quiet         only report the verdict
  -h, --help          this help

At least one of --actual or --kernel-dir is required.

Exit 0 match, 1 mismatch, 2 usage error.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --expected)   EXPECTED="${2:?--expected needs a value}"; shift 2 ;;
        --actual)     ACTUAL="${2:?--actual needs a value}"; shift 2 ;;
        --kernel-dir) KERNEL_DIR="${2:?--kernel-dir needs a value}"; shift 2 ;;
        --ref)        REF="${2:?--ref needs a value}"; shift 2 ;;
        -q|--quiet)   QUIET=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

[ -n "$EXPECTED" ] || die "--expected is required"
[ -f "$EXPECTED" ] || die "expected tested-set not found: $EXPECTED"
[ -n "$ACTUAL" ] || [ -n "$KERNEL_DIR" ] || die "one of --actual or --kernel-dir is required"
[ -f "$HELPER" ] || die "helper not found: $HELPER"
command -v python3 >/dev/null 2>&1 || die "python3 not found in PATH"

FAILURES=0

field() {   # field <file> <key>
    awk -F'\t' -v k="$2" '$1==k {print $2; exit}' "$1"
}

# --------------------------------------------------------------------------
step "Checking the expected record is self-consistent"

if python3 "$HELPER" digest "$EXPECTED" >/dev/null 2>&1; then
    info "expected digest matches its own contents"
else
    printf 'expected tested-set is internally inconsistent: %s\n' "$EXPECTED" >&2
    printf 'it was edited or truncated after being written\n' >&2
    FAILURES=$((FAILURES + 1))
fi

EXP_DIGEST="$(python3 "$HELPER" digest "$EXPECTED" --quiet 2>/dev/null || echo "")"
EXP_BASE="$(field "$EXPECTED" base)"
EXP_HEAD="$(field "$EXPECTED" generated_head)"
EXP_ORDER="$(field "$EXPECTED" order)"
EXP_TOPICS=$(awk -F'\t' '$1=="topic"' "$EXPECTED" | grep -c . || true)

info "expected digest : ${EXP_DIGEST:0:32}"
info "expected base   : ${EXP_BASE:0:12}"
info "expected topics : ${EXP_TOPICS:-0}"
[ -n "$EXP_HEAD" ] && info "tested head     : ${EXP_HEAD:0:12}"

# --------------------------------------------------------------------------
if [ -n "$ACTUAL" ]; then
    step "Comparing tested-set records"
    [ -f "$ACTUAL" ] || die "actual tested-set not found: $ACTUAL"

    if ! python3 "$HELPER" digest "$ACTUAL" >/dev/null 2>&1; then
        printf 'actual tested-set is internally inconsistent: %s\n' "$ACTUAL" >&2
        FAILURES=$((FAILURES + 1))
    fi

    ACT_DIGEST="$(python3 "$HELPER" digest "$ACTUAL" --quiet 2>/dev/null || echo "")"
    info "actual digest   : ${ACT_DIGEST:0:32}"

    if [ -n "$EXP_DIGEST" ] && [ "$EXP_DIGEST" = "$ACT_DIGEST" ]; then
        info "digests match: the regenerated tree consumes identical inputs"
    else
        printf 'digest mismatch\n  expected %s\n  actual   %s\n' \
            "$EXP_DIGEST" "$ACT_DIGEST" >&2
        FAILURES=$((FAILURES + 1))

        # A bare digest mismatch is useless to a human, so name what moved.
        exp_t="$(mktemp)"; act_t="$(mktemp)"
        awk -F'\t' '$1=="topic" {print $2"\t"$3}' "$EXPECTED" | sort > "$exp_t"
        awk -F'\t' '$1=="topic" {print $2"\t"$3}' "$ACTUAL"   | sort > "$act_t"

        printf '\ntopics only in expected:\n' >&2
        comm -23 <(cut -f1 "$exp_t") <(cut -f1 "$act_t") | sed 's/^/  - /' >&2 || true
        printf 'topics only in actual:\n' >&2
        comm -13 <(cut -f1 "$exp_t") <(cut -f1 "$act_t") | sed 's/^/  + /' >&2 || true
        printf 'topics whose sha changed:\n' >&2
        join -t$'\t' "$exp_t" "$act_t" 2>/dev/null \
            | awk -F'\t' '$2!=$3 {printf "  ~ %s: %s -> %s\n", $1, substr($2,1,12), substr($3,1,12)}' >&2 || true

        for key in base base_branch base_tag rerere_repo rerere_ref rerere_sha order; do
            e="$(field "$EXPECTED" "$key")"
            a="$(field "$ACTUAL" "$key")"
            if [ "$e" != "$a" ]; then
                printf '  ~ %s: %s -> %s\n' "$key" "${e:-<empty>}" "${a:-<empty>}" >&2
            fi
        done
        rm -f "$exp_t" "$act_t"
    fi
fi

# --------------------------------------------------------------------------
if [ -n "$KERNEL_DIR" ]; then
    step "Verifying the git tree contains the tested set"
    command -v git >/dev/null 2>&1 || die "git not found in PATH"
    git -C "$KERNEL_DIR" rev-parse --git-dir >/dev/null 2>&1 \
        || die "not a git repository: $KERNEL_DIR"

    if ! TREE_SHA="$(git -C "$KERNEL_DIR" rev-parse --verify "$REF^{commit}" 2>/dev/null)"; then
        die "ref not found in $KERNEL_DIR: $REF"
    fi
    info "verifying ref   : $REF @ ${TREE_SHA:0:12}"

    if [ -n "$EXP_HEAD" ] && [ "$EXP_HEAD" != "$TREE_SHA" ]; then
        # Expected: merge commit timestamps differ between runs. Content is what
        # must match, and that is checked below.
        info "note: head differs from the tested head (${EXP_HEAD:0:12}); comparing content instead"
    fi

    if [ -n "$EXP_BASE" ]; then
        if git -C "$KERNEL_DIR" merge-base --is-ancestor "$EXP_BASE" "$TREE_SHA" 2>/dev/null; then
            info "base ${EXP_BASE:0:12} is an ancestor"
        else
            printf 'tested base %s is not an ancestor of %s\n' \
                "${EXP_BASE:0:12}" "${TREE_SHA:0:12}" >&2
            FAILURES=$((FAILURES + 1))
        fi
    fi

    MISSING=0
    CHECKED=0
    while IFS=$'\t' read -r kind name sha; do
        [ "$kind" = "topic" ] || continue
        CHECKED=$((CHECKED + 1))
        if ! git -C "$KERNEL_DIR" cat-file -e "${sha}^{commit}" 2>/dev/null; then
            printf 'commit absent from the repository: %-28s %s\n' "$name" "${sha:0:12}" >&2
            MISSING=$((MISSING + 1))
            continue
        fi
        if ! git -C "$KERNEL_DIR" merge-base --is-ancestor "$sha" "$TREE_SHA" 2>/dev/null; then
            printf 'tested pin not in the tree: %-28s %s\n' "$name" "${sha:0:12}" >&2
            MISSING=$((MISSING + 1))
        fi
    done < <(awk -F'\t' '$1=="topic"' "$EXPECTED")

    info "pins checked    : $CHECKED"
    if [ "$MISSING" -gt 0 ]; then
        printf '%d tested pin(s) are not present in %s\n' "$MISSING" "$REF" >&2
        FAILURES=$((FAILURES + 1))
    else
        info "every tested pin is present in $REF"
    fi

    # Catch the opposite error: content in the tree that the tested set does not
    # account for. An unexpected merge parent means the tree is not the tested
    # tree even though every expected pin is present.
    EXTRA_PARENTS=0
    exp_shas="$(mktemp)"
    awk -F'\t' '$1=="topic" {print $3}' "$EXPECTED" | sort -u > "$exp_shas"
    echo "$EXP_BASE" >> "$exp_shas"
    sort -u -o "$exp_shas" "$exp_shas"

    while read -r mc; do
        [ -n "$mc" ] || continue
        # Second parent of each merge commit is the topic that was merged in.
        p2="$(git -C "$KERNEL_DIR" rev-parse --verify --quiet "${mc}^2" 2>/dev/null || echo "")"
        [ -n "$p2" ] || continue
        if ! grep -qxF "$p2" "$exp_shas"; then
            subj="$(git -C "$KERNEL_DIR" log -1 --format=%s "$mc" 2>/dev/null || echo "")"
            printf 'unexpected merge parent %s in %s (%s)\n' "${p2:0:12}" "${mc:0:12}" "$subj" >&2
            EXTRA_PARENTS=$((EXTRA_PARENTS + 1))
        fi
    done < <(git -C "$KERNEL_DIR" rev-list --merges "${EXP_BASE}..${TREE_SHA}" 2>/dev/null || true)
    rm -f "$exp_shas"

    if [ "$EXTRA_PARENTS" -gt 0 ]; then
        printf '%d merge parent(s) are not accounted for by the tested set\n' "$EXTRA_PARENTS" >&2
        FAILURES=$((FAILURES + 1))
    else
        info "no unaccounted merge parents"
    fi
fi

# --------------------------------------------------------------------------
step "Result"
if [ "$FAILURES" -eq 0 ]; then
    info "RESULT: PASS - the tree matches the tested set"
    [ "$QUIET" -eq 1 ] && echo "PASS"
    exit 0
fi
printf 'RESULT: FAIL - %d check(s) failed; publication must not proceed\n' "$FAILURES" >&2
exit 1
