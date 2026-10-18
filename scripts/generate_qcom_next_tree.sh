#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# Generate the candidate qcom-next tree from the SHA-pinned configuration.
#
# The tree is a deterministic function of the config:
#
#     qcom-next = f( base.sha , ordered topics[].sha , rerere cache revision )
#
# How the pinning works
# ---------------------
# automerge's ci-merge consumes a flat three-column conf and merges
# <remote>/<branch>, i.e. live branch tips. Two properties of ci-merge let us
# feed it pinned commits without rewriting it:
#
#   1. It prefers a local ref named override/<remote-name> over
#      <remote-name>/<branch> (ci-merge:645-650). Creating override/<topic> at
#      the pinned SHA is therefore enough to pin every topic.
#
#   2. It derives the merge base from remotes/<baseline>/<branch> and checks the
#      integration branch out at that commit. There is no override hook for the
#      baseline, so the only way to pin base.sha is for the baseline remote to be
#      a repository whose branch already sits at that commit.
#
# Both are satisfied by rewriting the conf so every row points at the local
# kernel worktree over file://, after resetting local branches in that worktree
# to the pinned SHAs. That pins the baseline, pins the topics, and turns 78
# network fetches into local ones.
#
# Usage:
#   generate_qcom_next_tree.sh --kernel-dir DIR [options]
#
# Exit status:
#   0  candidate tree generated, every pin verified present in its history
#   1  generation failed: unresolved conflict, missing pin, or verification gap
#   2  usage error or missing prerequisite
set -u
set -o pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SELF_DIR/.." && pwd)"
HELPER="$SELF_DIR/qcom_next_config.py"

CONFIG="$REPO_DIR/qcom-next.yaml"
KERNEL_DIR=""
AUTOMERGE_DIR=""
INTEG_BRANCH="qcom-next"
OUT_DIR=""
BASELINE_NAME="baseline"
RERERE_URL=""
SKIP_FETCH=0
ALLOW_CONFLICTS=0
ALLOW_DIRTY=0
KEEP_REMOTES=0
DRY_RUN=0
NO_RERERE=0
VERBOSE=0

die()  { printf 'error: %s\n' "$*" >&2; exit 2; }
fail() { printf 'FAILED: %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
dbg()  { [ "$VERBOSE" -eq 1 ] && printf '    %s\n' "$*" || true; }

# ci-merge's own output is the only record of why a merge failed, so show it on
# every failure path rather than only pointing at a file that a CI runner
# discards when the job ends.
dump_merge_tail() {
    [ -n "${MERGE_LOG:-}" ] && [ -f "$MERGE_LOG" ] || return 0
    printf '\n--- last 60 lines of %s ---\n' "$MERGE_LOG" >&2
    tail -60 "$MERGE_LOG" >&2
    printf -- '--- end of ci-merge output ---\n' >&2
}

usage() {
    sed -n '3,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --config PATH         SHA-pinned config (default: <repo>/qcom-next.yaml)
  --kernel-dir DIR      kernel worktree to build the tree in (required)
  --automerge DIR       directory containing ci-merge
                        (default: <kernel-dir>/../automerge)
  --integ-branch NAME   integration branch to create (default: qcom-next)
  --out-dir DIR         artifact directory
                        (default: <kernel-dir>/../qcom-next-candidate)
  --baseline-name NAME  conf row name ci-merge treats as baseline
                        (default: baseline)
  --rerere-url URL      override the rerere cache repo from the config
  --no-rerere           run without a rerere cache (every conflict surfaces)
  --skip-fetch          do not contact remotes; all pins must already be local
  --allow-conflicts     report conflicts but still exit 0 (debugging only)
  --allow-dirty         proceed even if the kernel worktree is dirty
  --keep-remotes        do not remove remotes that are absent from the conf
  --dry-run             prepare refs and conf, but do not run ci-merge
  -v, --verbose         more detail
  -h, --help            this help

Artifacts written to --out-dir:
  merge.conf        generated flat conf (disposable; never commit it)
  merge.log         full ci-merge output
  tested-set.txt    canonical record of the inputs, with digest
  topic-shas.tsv    topic -> pinned sha, merged commit count
  conflicts.txt     conflicting topics and paths, when any
  summary.md        Markdown summary for a PR comment or job summary
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --config)        CONFIG="${2:?--config needs a value}"; shift 2 ;;
        --kernel-dir)    KERNEL_DIR="${2:?--kernel-dir needs a value}"; shift 2 ;;
        --automerge)     AUTOMERGE_DIR="${2:?--automerge needs a value}"; shift 2 ;;
        --integ-branch)  INTEG_BRANCH="${2:?--integ-branch needs a value}"; shift 2 ;;
        --out-dir)       OUT_DIR="${2:?--out-dir needs a value}"; shift 2 ;;
        --baseline-name) BASELINE_NAME="${2:?--baseline-name needs a value}"; shift 2 ;;
        --rerere-url)    RERERE_URL="${2:?--rerere-url needs a value}"; shift 2 ;;
        --no-rerere)     NO_RERERE=1; shift ;;
        --skip-fetch)    SKIP_FETCH=1; shift ;;
        --allow-conflicts) ALLOW_CONFLICTS=1; shift ;;
        --allow-dirty)   ALLOW_DIRTY=1; shift ;;
        --keep-remotes)  KEEP_REMOTES=1; shift ;;
        --dry-run)       DRY_RUN=1; shift ;;
        -v|--verbose)    VERBOSE=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

# --------------------------------------------------------------------------
step "Checking prerequisites"

[ -n "$KERNEL_DIR" ] || die "--kernel-dir is required"
[ -f "$CONFIG" ]     || die "config not found: $CONFIG"
[ -f "$HELPER" ]     || die "helper not found: $HELPER"
command -v git >/dev/null 2>&1       || die "git not found in PATH"
command -v python3 >/dev/null 2>&1   || die "python3 not found in PATH"

KERNEL_DIR="$(cd "$KERNEL_DIR" 2>/dev/null && pwd)" || die "cannot enter --kernel-dir"

# ci-merge greps the conf for the remote URL unquoted, so a path containing
# whitespace or a glob character would corrupt its stale-remote detection.
case "$KERNEL_DIR" in
    *[[:space:]]*) die "--kernel-dir path contains whitespace, which ci-merge cannot handle: $KERNEL_DIR" ;;
    *'*'*|*'?'*|*'['*) die "--kernel-dir path contains a glob character: $KERNEL_DIR" ;;
esac

git -C "$KERNEL_DIR" rev-parse --git-dir >/dev/null 2>&1 \
    || die "--kernel-dir is not a git repository: $KERNEL_DIR"
[ -f "$KERNEL_DIR/Makefile" ] \
    || die "--kernel-dir does not look like a kernel tree (no Makefile): $KERNEL_DIR"

if [ -z "$AUTOMERGE_DIR" ]; then
    AUTOMERGE_DIR="$(cd "$KERNEL_DIR/.." 2>/dev/null && pwd)/automerge"
fi
CI_MERGE="$AUTOMERGE_DIR/ci-merge"
[ -x "$CI_MERGE" ] || [ -f "$CI_MERGE" ] || die "ci-merge not found at $CI_MERGE (use --automerge)"

# ci-merge varies between automerge revisions, and depending on a feature that
# only some of them have is how this script first broke in CI: it hardcoded -p,
# which exists in some forks but not in qualcomm-linux/automerge, and getopt
# rejected it with "invalid option -- 'p'" before anything ran. Probe instead.
#
#   --pass/-p  continue past a conflicting topic so one run reports every
#              conflict. Without it ci-merge stops at the first one.
#   "Merge successful : <name> : <sha> : <n>" and "Merge conflict : ..." are
#              emitted only by revisions carrying the log-parser support.
#              Others print just "Merge failed, <name> <url> <branch>".
CI_MERGE_HAS_PASS=0
CI_MERGE_HAS_SUMMARY=0
grep -qE 'LONGOPT=.*[,"]pass' "$CI_MERGE" && CI_MERGE_HAS_PASS=1
grep -q 'Merge successful : ' "$CI_MERGE" && CI_MERGE_HAS_SUMMARY=1

if [ -z "$OUT_DIR" ]; then
    OUT_DIR="$(cd "$KERNEL_DIR/.." && pwd)/qcom-next-candidate"
fi
mkdir -p "$OUT_DIR" || die "cannot create --out-dir: $OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

MERGE_CONF="$OUT_DIR/merge.conf"
MERGE_LOG="$OUT_DIR/merge.log"
TESTED_SET="$OUT_DIR/tested-set.txt"
TOPIC_SHAS="$OUT_DIR/topic-shas.tsv"
CONFLICTS="$OUT_DIR/conflicts.txt"
SUMMARY="$OUT_DIR/summary.md"

hlp() { python3 "$HELPER" --config "$CONFIG" "$@"; }

# The helper writes "-" for an empty field. Tab is IFS whitespace, so bash
# collapses consecutive tabs; a blank column would shift every later field and a
# null tag could be mistaken for a sha. Decode the sentinel explicitly.
unsent() { [ "${1:-}" = "-" ] && printf '' || printf '%s' "${1:-}"; }

info "config      : $CONFIG"
info "kernel dir  : $KERNEL_DIR"
info "automerge   : $CI_MERGE"
info "  --pass    : $([ "$CI_MERGE_HAS_PASS" -eq 1 ] && echo 'supported' || echo 'not supported; stops at the first conflict')"
info "  summary   : $([ "$CI_MERGE_HAS_SUMMARY" -eq 1 ] && echo 'emits per-topic merge lines' || echo 'no per-topic lines; relying on pin verification')"
info "integ branch: $INTEG_BRANCH"
info "out dir     : $OUT_DIR"

# --------------------------------------------------------------------------
step "Reading configuration"

IFS=$'\t' read -r BASE_REPO BASE_BRANCH BASE_TAG BASE_SHA < <(hlp base) \
    || die "cannot read base from config"
BASE_REPO="$(unsent "$BASE_REPO")"
BASE_BRANCH="$(unsent "$BASE_BRANCH")"
BASE_TAG="$(unsent "$BASE_TAG")"
BASE_SHA="$(unsent "$BASE_SHA")"
[ -n "$BASE_SHA" ]    || fail "base.sha is empty; the baseline must be pinned to a concrete commit"
[ -n "$BASE_BRANCH" ] || fail "base.branch is empty"

IFS=$'\t' read -r RR_REPO RR_REF RR_SHA < <(hlp rerere) || die "cannot read rerere_cache"
RR_REPO="$(unsent "$RR_REPO")"
RR_REF="$(unsent "$RR_REF")"
RR_SHA="$(unsent "$RR_SHA")"
[ -n "$RERERE_URL" ] && RR_REPO="$RERERE_URL"

TOPICS_TSV="$OUT_DIR/.topics.tsv"
hlp topics > "$TOPICS_TSV" || die "cannot read topics from config"
TOPIC_COUNT=$(grep -c . < "$TOPICS_TSV" || true)
[ "${TOPIC_COUNT:-0}" -gt 0 ] || fail "config lists no topics"

info "base        : $BASE_BRANCH @ ${BASE_SHA:0:12} (tag: ${BASE_TAG:-none})"
info "rerere      : ${RR_REPO:-none} @ ${RR_REF:-none}"
info "topics      : $TOPIC_COUNT"

# Every pin must be concrete before we touch the tree.
MISSING_PINS=0
while IFS=$'\t' read -r name repo branch sha tag; do
    name="$(unsent "$name")"; sha="$(unsent "$sha")"
    [ -n "$name" ] || continue
    if [ -z "$sha" ]; then
        printf 'unpinned topic: %s (sha is null)\n' "$name" >&2
        MISSING_PINS=$((MISSING_PINS + 1))
    fi
done < "$TOPICS_TSV"
[ "$MISSING_PINS" -eq 0 ] \
    || fail "$MISSING_PINS topic(s) have no pinned sha; resolve them to concrete commits first"

step "Checking for git ref name collisions"
hlp ref-clashes || fail "ref name collisions would make the pin refs unrepresentable"

# --------------------------------------------------------------------------
step "Preparing the kernel worktree"

if [ -n "$(git -C "$KERNEL_DIR" status --porcelain)" ]; then
    if [ "$ALLOW_DIRTY" -eq 1 ]; then
        info "worktree is dirty; continuing because --allow-dirty was given"
    else
        git -C "$KERNEL_DIR" status --short >&2
        fail "kernel worktree is dirty. ci-merge would offer to hard-reset it; refusing to guess. Clean it or pass --allow-dirty"
    fi
fi

# ci-merge renames an existing integration branch, which accumulates clutter
# over repeated CI runs, and it cannot check out a branch that is checked out.
CURRENT_BRANCH="$(git -C "$KERNEL_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"

# Detach HEAD before touching any refs. The pin refs below include
# refs/heads/<base-branch> and refs/heads/<topic-branch>, any of which may be
# the branch currently checked out. git 2.45 and later refuse to update the ref
# of a checked-out branch, so this is required for the update-ref batch to
# apply; older git allowed it, which quietly left the index and worktree
# disagreeing with the ref. ci-merge checks out its own integration branch
# shortly afterwards, so a detached HEAD here is harmless.
if [ "$CURRENT_BRANCH" != "HEAD" ]; then
    info "detaching HEAD (was on '$CURRENT_BRANCH') so branch refs can be repointed"
    git -C "$KERNEL_DIR" checkout --quiet --detach HEAD \
        || fail "cannot detach HEAD in $KERNEL_DIR"
fi

if git -C "$KERNEL_DIR" show-ref --verify --quiet "refs/heads/$INTEG_BRANCH"; then
    info "deleting stale integration branch '$INTEG_BRANCH' from a previous run"
    git -C "$KERNEL_DIR" branch -D "$INTEG_BRANCH" >/dev/null || fail "cannot delete $INTEG_BRANCH"
fi

# --------------------------------------------------------------------------
step "Fetching pinned commits"

if [ "$SKIP_FETCH" -eq 1 ]; then
    info "--skip-fetch: assuming every pinned commit is already local"
else
    # One fetch per distinct repository rather than per topic. Branches and tags
    # are fetched into a private namespace so they cannot disturb the local
    # branches we are about to point at the pins.
    REPOS="$(awk -F'\t' 'NF>1 && $2!="-" {print $2}' "$TOPICS_TSV" | sort -u)"
    ALL_REPOS="$(printf '%s\n%s\n' "$BASE_REPO" "$REPOS" | sed '/^$/d' | sort -u)"
    for repo in $ALL_REPOS; do
        info "fetching $repo"
        if ! git -C "$KERNEL_DIR" fetch --quiet --no-tags --force "$repo" \
                "+refs/heads/*:refs/qcom-next-src/heads/*" \
                "+refs/tags/*:refs/qcom-next-src/tags/*" 2>&1; then
            info "  branch/tag fetch failed for $repo; individual pins will be retried directly"
        fi
    done
fi

# Verify every pin is present locally, retrying a direct SHA fetch if not.
step "Verifying pinned commits are present locally"

have_commit() {
    git -C "$KERNEL_DIR" cat-file -e "${1}^{commit}" 2>/dev/null
}

fetch_single_sha() {
    local repo="$1" sha="$2"
    [ "$SKIP_FETCH" -eq 1 ] && return 1
    git -C "$KERNEL_DIR" fetch --quiet --no-tags "$repo" "$sha" 2>/dev/null
}

PIN_ERRORS=0
if ! have_commit "$BASE_SHA"; then
    if ! fetch_single_sha "$BASE_REPO" "$BASE_SHA" || ! have_commit "$BASE_SHA"; then
        printf 'missing base.sha %s in %s\n' "$BASE_SHA" "$BASE_REPO" >&2
        PIN_ERRORS=$((PIN_ERRORS + 1))
    fi
fi
while IFS=$'\t' read -r name repo branch sha tag; do
    name="$(unsent "$name")"; repo="$(unsent "$repo")"; sha="$(unsent "$sha")"
    [ -n "$name" ] || continue
    if ! have_commit "$sha"; then
        if ! fetch_single_sha "$repo" "$sha" || ! have_commit "$sha"; then
            printf 'missing pin for %-28s %s\n' "$name" "$sha" >&2
            PIN_ERRORS=$((PIN_ERRORS + 1))
        fi
    fi
done < "$TOPICS_TSV"

if [ "$PIN_ERRORS" -gt 0 ]; then
    fail "$PIN_ERRORS pinned commit(s) are unreachable. A pin usually disappears because the topic branch was rebased and the commit was never tagged (FR-INT-01). Tag the consumed commit, or re-pin to one that still exists."
fi
info "all $((TOPIC_COUNT + 1)) pinned commits present"

# --------------------------------------------------------------------------
step "Creating pin refs"

# refs/heads/<branch>        resolvable branch for the file:// remote; this is
#                            what pins the baseline, since ci-merge takes the
#                            merge base from remotes/<baseline>/<branch>
# refs/heads/override/<name> checked first by ci-merge, so it is the
#                            authoritative topic pin
PIN_REFS="$OUT_DIR/.pin-refs.tsv"
hlp pin-refs > "$PIN_REFS" || fail "cannot compute pin refs"

# git update-ref --stdin applies the whole batch atomically.
UPDATE_IN="$OUT_DIR/.update-ref.in"
awk -F'\t' 'NF==2 {printf "update %s %s\n", $1, $2}' "$PIN_REFS" > "$UPDATE_IN"
PIN_REF_COUNT=$(grep -c . < "$UPDATE_IN" || true)
if ! UPDATE_ERR="$(git -C "$KERNEL_DIR" update-ref --stdin < "$UPDATE_IN" 2>&1)"; then
    printf '%s\n' "$UPDATE_ERR" >&2
    printf 'HEAD is at: %s\n' \
        "$(git -C "$KERNEL_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)" >&2
    fail "cannot create pin refs (batch in $UPDATE_IN). A common cause is a branch being checked out while its ref is repointed; git 2.45+ refuses that."
fi
info "created/updated $PIN_REF_COUNT pin refs"

dbg "baseline pin: refs/heads/$BASE_BRANCH -> ${BASE_SHA:0:12}"

# --------------------------------------------------------------------------
step "Generating merge.conf"

hlp merge-conf --url-override "file://$KERNEL_DIR" \
               --baseline-name "$BASELINE_NAME" \
               --output "$MERGE_CONF" \
    || fail "cannot generate merge.conf"

# --------------------------------------------------------------------------
step "Reconciling git remotes"

# ci-merge prompts once per remote that is absent from the conf. Its prompt is
# read with plain `read`, and on EOF the empty answer counts as "yes, delete".
# Rather than depend on that, remove them explicitly and visibly.
CONF_NAMES="$OUT_DIR/.conf-names.txt"
grep -v '^[[:space:]]*#' "$MERGE_CONF" | awk 'NF>=3 {print $1}' | sort -u > "$CONF_NAMES"

if [ "$KEEP_REMOTES" -eq 1 ]; then
    info "--keep-remotes: leaving existing remotes in place (ci-merge may prompt)"
else
    REMOVED=0
    while read -r rname; do
        [ -n "$rname" ] || continue
        if ! grep -qxF "$rname" "$CONF_NAMES"; then
            dbg "removing remote not in conf: $rname"
            git -C "$KERNEL_DIR" remote remove "$rname" 2>/dev/null && REMOVED=$((REMOVED + 1))
        fi
    done < <(git -C "$KERNEL_DIR" remote)
    info "removed $REMOVED remote(s) absent from the conf"
fi

# Point every conf remote at the local worktree. ci-merge adds missing remotes
# itself, but a leftover remote with the old https URL would make its
# stale-remote check disagree with the conf.
RETARGETED=0
while read -r rname; do
    [ -n "$rname" ] || continue
    if git -C "$KERNEL_DIR" remote get-url "$rname" >/dev/null 2>&1; then
        cur="$(git -C "$KERNEL_DIR" remote get-url "$rname")"
        if [ "$cur" != "file://$KERNEL_DIR" ]; then
            git -C "$KERNEL_DIR" remote set-url "$rname" "file://$KERNEL_DIR" \
                && RETARGETED=$((RETARGETED + 1))
        fi
    fi
done < "$CONF_NAMES"
info "retargeted $RETARGETED existing remote(s) to file://$KERNEL_DIR"

if [ "$DRY_RUN" -eq 1 ]; then
    step "Dry run: stopping before ci-merge"
    info "merge.conf : $MERGE_CONF"
    info "pin refs   : $PIN_REFS"
    exit 0
fi

# --------------------------------------------------------------------------
step "Running ci-merge"

CI_ARGS=(-f "$MERGE_CONF" -t head -i "$INTEG_BRANCH" -b "$BASELINE_NAME")
if [ "$CI_MERGE_HAS_PASS" -eq 1 ]; then
    # Continue past a conflicting topic so a single run reports every conflict.
    # We still fail afterwards unless --allow-conflicts.
    CI_ARGS+=(-p)
else
    info "this ci-merge has no --pass, so it will stop at the first conflicting topic"
fi
if [ "$NO_RERERE" -eq 0 ] && [ -n "$RR_REPO" ]; then
    CI_ARGS+=(-c "$RR_REPO")
else
    info "running without a rerere cache; previously recorded resolutions will not apply"
fi

# Deliberately not passing -n: interactive mode would invoke git mergetool.
# -p (BYPASS) makes ci-merge continue past a conflicting topic so a single run
# reports every conflict; we still fail afterwards unless --allow-conflicts.
# stdin is /dev/null so any unexpected prompt cannot hang CI.
info "command: $CI_MERGE ${CI_ARGS[*]}"
: > "$MERGE_LOG"
( cd "$KERNEL_DIR" && bash "$CI_MERGE" "${CI_ARGS[@]}" ) < /dev/null 2>&1 | tee -a "$MERGE_LOG"
CI_RC=${PIPESTATUS[0]}
info "ci-merge exit status: $CI_RC"

# --------------------------------------------------------------------------
step "Parsing merge results"

MERGED_COUNT=$(grep -c '^Merge successful : ' "$MERGE_LOG" 2>/dev/null || true)
NOTHING_COUNT=$(grep -c '^Nothing to merge' "$MERGE_LOG" 2>/dev/null || true)
# Two formats, depending on the automerge revision:
#   "Merge conflict : <name> : <sha>"        with log-parser support
#   "Merge failed, <name> <url> <branch>"    everywhere
# Count both so a conflict is never silently reported as zero.
CONFLICT_COUNT=$(grep -c '^Merge conflict : ' "$MERGE_LOG" 2>/dev/null || true)
FAILED_COUNT=$(grep -c '^Merge failed, ' "$MERGE_LOG" 2>/dev/null || true)
MERGED_COUNT=${MERGED_COUNT:-0}
NOTHING_COUNT=${NOTHING_COUNT:-0}
CONFLICT_COUNT=${CONFLICT_COUNT:-0}
FAILED_COUNT=${FAILED_COUNT:-0}
CONFLICT_TOTAL=$((CONFLICT_COUNT + FAILED_COUNT))

: > "$CONFLICTS"
if [ "$CONFLICT_TOTAL" -gt 0 ]; then
    {
        echo "# Topics whose merge could not be resolved"
        if [ "$CONFLICT_COUNT" -gt 0 ]; then
            grep '^Merge conflict : ' "$MERGE_LOG" \
                | awk -F' : ' '{printf "topic\t%s\t%s\n", $2, $3}'
        fi
        if [ "$FAILED_COUNT" -gt 0 ]; then
            # "Merge failed, <name> <url> <branch>"
            grep '^Merge failed, ' "$MERGE_LOG" \
                | sed 's/^Merge failed, //' \
                | awk '{printf "topic\t%s\t%s\n", $1, "unknown"}'
        fi
        # Present only when ci-merge echoes the failed merge output.
        if grep -q '^CONFLICT' "$MERGE_LOG"; then
            echo "# Conflicting paths reported by git"
            grep '^CONFLICT' "$MERGE_LOG" \
                | sed -e 's/.*Merge conflict in /path\t/' -e 's/^CONFLICT[^:]*: /note\t/' \
                | sort -u
        fi
    } > "$CONFLICTS"
fi

info "merged      : $MERGED_COUNT"
info "up to date  : $NOTHING_COUNT"
info "conflicts   : $CONFLICT_TOTAL"
if [ "$CI_MERGE_HAS_SUMMARY" -eq 0 ]; then
    info "note: this ci-merge emits no per-topic merge lines, so the merged count"
    info "      is not meaningful; pin verification below is authoritative"
fi

if ! git -C "$KERNEL_DIR" show-ref --verify --quiet "refs/heads/$INTEG_BRANCH"; then
    dump_merge_tail
    fail "ci-merge did not create the integration branch '$INTEG_BRANCH'"
fi

INTEG_SHA="$(git -C "$KERNEL_DIR" rev-parse "refs/heads/$INTEG_BRANCH")"
info "integration : $INTEG_BRANCH @ ${INTEG_SHA:0:12}"

# --------------------------------------------------------------------------
step "Verifying the generated tree contains exactly the pinned commits"

# This is the real proof that pinning worked: every pinned commit must be an
# ancestor of the integration head, and the baseline must be too. A branch-tip
# regression would show up here as a pin that is not an ancestor.
: > "$TOPIC_SHAS"
printf 'name\tpinned_sha\tcommits\tstatus\n' >> "$TOPIC_SHAS"

VERIFY_ERRORS=0

if git -C "$KERNEL_DIR" merge-base --is-ancestor "$BASE_SHA" "$INTEG_SHA" 2>/dev/null; then
    dbg "base.sha is an ancestor of the integration head"
else
    printf 'base.sha %s is not an ancestor of %s\n' "${BASE_SHA:0:12}" "${INTEG_SHA:0:12}" >&2
    VERIFY_ERRORS=$((VERIFY_ERRORS + 1))
fi

CONFLICTED_NAMES="$OUT_DIR/.conflicted-names.txt"
grep '^Merge conflict : ' "$MERGE_LOG" 2>/dev/null \
    | awk -F' : ' '{print $2}' | sed 's/[[:space:]]*$//' | sort -u > "$CONFLICTED_NAMES" || true
[ -f "$CONFLICTED_NAMES" ] || : > "$CONFLICTED_NAMES"

while IFS=$'\t' read -r name repo branch sha tag; do
    name="$(unsent "$name")"; sha="$(unsent "$sha")"
    [ -n "$name" ] || continue
    if grep -qxF "$name" "$CONFLICTED_NAMES" 2>/dev/null; then
        printf '%s\t%s\t0\tconflict\n' "$name" "$sha" >> "$TOPIC_SHAS"
        continue
    fi
    if git -C "$KERNEL_DIR" merge-base --is-ancestor "$sha" "$INTEG_SHA" 2>/dev/null; then
        n=$(git -C "$KERNEL_DIR" rev-list --no-merges --count "$BASE_SHA..$sha" 2>/dev/null || echo 0)
        printf '%s\t%s\t%s\tmerged\n' "$name" "$sha" "$n" >> "$TOPIC_SHAS"
    else
        printf '%s\t%s\t0\tMISSING\n' "$name" "$sha" >> "$TOPIC_SHAS"
        printf 'pinned commit for %-28s is not in the generated tree: %s\n' "$name" "$sha" >&2
        VERIFY_ERRORS=$((VERIFY_ERRORS + 1))
    fi
done < "$TOPICS_TSV"

VERIFIED=$(awk -F'\t' '$4=="merged"' "$TOPIC_SHAS" | grep -c . || true)
info "pins verified present: ${VERIFIED:-0} / $TOPIC_COUNT"

# --------------------------------------------------------------------------
step "Recording the tested set"

hlp tested-set --generated-head "$INTEG_SHA" --output "$TESTED_SET" \
    || fail "cannot write the tested set"
DIGEST="$(python3 "$HELPER" digest "$TESTED_SET" --quiet)"
info "digest: $DIGEST"

{
    echo "### qcom-next candidate tree"
    echo ""
    echo "| Field | Value |"
    echo "|---|---|"
    echo "| Integration branch | \`$INTEG_BRANCH\` |"
    echo "| Generated head | \`$INTEG_SHA\` |"
    echo "| Base | \`$BASE_BRANCH\` @ \`${BASE_SHA:0:12}\` (tag \`${BASE_TAG:-none}\`) |"
    echo "| Rerere cache | \`${RR_REPO:-none}\` @ \`${RR_REF:-none}\` |"
    echo "| Topics configured | $TOPIC_COUNT |"
    echo "| Topics merged | $MERGED_COUNT |"
    echo "| Already up to date | $NOTHING_COUNT |"
    echo "| Conflicts | $CONFLICT_TOTAL |"
    echo "| Pins verified | ${VERIFIED:-0} / $TOPIC_COUNT |"
    echo "| Tested-set digest | \`$DIGEST\` |"
    echo ""
    if [ "$CONFLICT_TOTAL" -gt 0 ]; then
        echo "#### Unresolved conflicts"
        echo ""
        echo "The rerere cache has no recorded resolution for these topics."
        echo "Resolve them locally with the same config and cache revision, then"
        echo "push the updated rerere cache to this PR."
        echo ""
        awk -F'\t' '$1=="topic" {printf "- `%s` at `%s`\n", $2, substr($3,1,12)}' "$CONFLICTS"
        echo ""
        if awk -F'\t' '$1=="path"' "$CONFLICTS" | grep -q .; then
            echo "Conflicting paths:"
            echo ""
            awk -F'\t' '$1=="path" {printf "- `%s`\n", $2}' "$CONFLICTS" | sort -u
            echo ""
        fi
    fi
} > "$SUMMARY"

rm -f "$OUT_DIR/.topics.tsv" "$OUT_DIR/.pin-refs.tsv" "$OUT_DIR/.update-ref.in" \
      "$OUT_DIR/.conf-names.txt" "$OUT_DIR/.conflicted-names.txt"

# --------------------------------------------------------------------------
step "Result"

info "artifacts in $OUT_DIR"
info "  merge.conf     generated conf (disposable)"
info "  merge.log      ci-merge output"
info "  tested-set.txt inputs + digest"
info "  topic-shas.tsv topic -> pinned sha"
[ "$CONFLICT_TOTAL" -gt 0 ] && info "  conflicts.txt  unresolved conflicts"
info "  summary.md     Markdown summary"

if [ "$VERIFY_ERRORS" -gt 0 ]; then
    dump_merge_tail
    fail "$VERIFY_ERRORS pin(s) are not present in the generated tree; the tree does not match the config"
fi

if [ "$CONFLICT_TOTAL" -gt 0 ]; then
    if [ "$ALLOW_CONFLICTS" -eq 1 ]; then
        info "RESULT: generated with $CONFLICT_TOTAL unresolved conflict(s) (--allow-conflicts)"
        exit 0
    fi
    cat "$CONFLICTS" >&2
    fail "$CONFLICT_TOTAL topic(s) could not be merged. See $CONFLICTS and $SUMMARY"
fi

if [ "$CI_RC" -ne 0 ]; then
    dump_merge_tail
    fail "ci-merge exited $CI_RC despite no reported conflict; see $MERGE_LOG"
fi

info "RESULT: PASS - candidate tree generated, all $TOPIC_COUNT pins verified"
exit 0
