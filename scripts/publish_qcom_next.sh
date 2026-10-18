#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# Publish the tested qcom-next candidate for the merged configuration.
#
# Runs after the integrator merges the config PR. It does not regenerate the
# tree: ci-merge stamps merge commits with the current time, so regenerating
# would produce a content-identical commit with a different id, and the commit
# that passed the gates is the one that should become qcom-next.
#
# The merged config is the only input. Its digest identifies the tested tree,
# which record_tested_candidate.sh published as
#
#     refs/qcom-next-tested/<digest>
#
# in the candidate repository. The first parent of that ref is the tested
# commit.
#
# Everything is verified before the ref moves:
#
#   1. the config is valid and promotion_state permits publishing
#   2. a manifest exists for the merged config's digest
#   3. the manifest is internally consistent and names a reachable commit
#   4. the required gate checks are green on that commit
#   5. the commit really contains the tested set
#   6. the push uses --force-with-lease against the observed head
#
# Any failure leaves qcom-next untouched. Because the merge happened first, that
# is a benign "needs republish" state that a re-run resolves.
#
# Exit status:
#   0  published, or already up to date
#   1  refused to publish, or the push failed
#   2  usage error or missing prerequisite
set -u
set -o pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SELF_DIR/qcom_next_config.py"
CHECKER="$SELF_DIR/check_tested_tree_matches.sh"

CONFIG=""
CANDIDATE_REPO="qualcomm-linux-stg/kernel"
PUBLISH_REPO="qualcomm-linux-stg/kernel"
PUBLISH_REF="qcom-next"
REF_NS="refs/qcom-next-tested"
REQUIRE_STATUSES="qcom-next-build"
WORK_DIR=""
DIGEST_OVERRIDE=""
OUTPUT=""
DRY_RUN=0
SKIP_STATUS=0
QUIET=0

die()  { printf 'error: %s\n' "$*" >&2; exit 2; }
fail() { printf '\nRESULT: FAIL - %s\n' "$*" >&2; exit 1; }
info() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }
step() { [ "$QUIET" -eq 1 ] || printf '\n==> %s\n' "$*"; }

usage() {
    cat <<'EOF'
Publish the tested qcom-next candidate for the merged configuration.

Options:
  --config PATH          merged qcom-next.yaml (required)
  --candidate-repo R     repo holding candidates and manifests
                         (default: qualcomm-linux-stg/kernel)
  --publish-repo R       repo whose branch is updated
                         (default: qualcomm-linux-stg/kernel)
  --publish-ref NAME     branch to update (default: qcom-next)
  --ref-namespace NS     manifest namespace (default: refs/qcom-next-tested)
  --require-statuses L   comma-separated checks that must be green on the
                         candidate (default: qcom-next-build; "" to skip)
  --digest SHA256        publish this digest instead of the config's
  --work-dir DIR         scratch clone location (default: mktemp)
  --output PATH          write a markdown summary here
  --dry-run              verify everything, do not push
  --skip-status-check    do not query commit statuses (offline tests)
  -q, --quiet            only report the verdict
  -h, --help             this help

Pushing requires GH_PUSH_TOKEN. Status checks require GH_TOKEN and gh.

Exit 0 published or already current, 1 refused/failed, 2 usage error.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --config)            CONFIG="${2:?}"; shift 2 ;;
        --candidate-repo)    CANDIDATE_REPO="${2:?}"; shift 2 ;;
        --publish-repo)      PUBLISH_REPO="${2:?}"; shift 2 ;;
        --publish-ref)       PUBLISH_REF="${2:?}"; shift 2 ;;
        --ref-namespace)     REF_NS="${2:?}"; shift 2 ;;
        --require-statuses)  REQUIRE_STATUSES="${2-}"; shift 2 ;;
        --digest)            DIGEST_OVERRIDE="${2:?}"; shift 2 ;;
        --work-dir)          WORK_DIR="${2:?}"; shift 2 ;;
        --output)            OUTPUT="${2:?}"; shift 2 ;;
        --dry-run)           DRY_RUN=1; shift ;;
        --skip-status-check) SKIP_STATUS=1; shift ;;
        -q|--quiet)          QUIET=1; shift ;;
        -h|--help)           usage; exit 0 ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

[ -n "$CONFIG" ] || die "--config is required"
[ -f "$CONFIG" ] || die "config not found: $CONFIG"
[ -f "$HELPER" ] || die "helper not found: $HELPER"
[ -f "$CHECKER" ] || die "checker not found: $CHECKER"
command -v git >/dev/null 2>&1     || die "git not found in PATH"
command -v python3 >/dev/null 2>&1 || die "python3 not found in PATH"
for r in "$CANDIDATE_REPO" "$PUBLISH_REPO"; do
    case "$r" in */*) ;; *) die "repository must be OWNER/REPO: $r" ;; esac
done

CLEAN_WORK=0
if [ -z "$WORK_DIR" ]; then
    WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/qcom-next-publish.XXXXXX")"
    CLEAN_WORK=1
fi
mkdir -p "$WORK_DIR"
cleanup() { [ "$CLEAN_WORK" -eq 1 ] && rm -rf "$WORK_DIR"; }
trap cleanup EXIT

KDIR="$WORK_DIR/kernel"

info "candidate repo : $CANDIDATE_REPO"
info "publish target : $PUBLISH_REPO @ $PUBLISH_REF"
info "config         : $CONFIG"
[ "$DRY_RUN" -eq 1 ] && info "mode           : dry run (no push)"

auth_url() {   # auth_url <owner/repo | url | path>
    case "$1" in
        # A full URL or a local path is used verbatim, with no token injected:
        # that is what lets this run against local repositories in tests, and
        # it avoids mangling an explicitly supplied remote.
        *://*|/*)
            printf '%s' "$1"
            ;;
        *)
            if [ -n "${GH_PUSH_TOKEN:-}" ]; then
                printf 'https://x-access-token:%s@github.com/%s.git' "$GH_PUSH_TOKEN" "$1"
            else
                printf 'https://github.com/%s.git' "$1"
            fi
            ;;
    esac
}
scrub() { sed 's/x-access-token:[^@]*@/x-access-token:***@/g'; }

# --------------------------------------------------------------------------
step "Validating the merged configuration"

if [ -f "$SELF_DIR/validate_qcom_next_config.py" ]; then
    if ! python3 "$SELF_DIR/validate_qcom_next_config.py" \
            --config "$CONFIG" --offline --allow-untagged --no-maintainer-check \
            >"$WORK_DIR/validate.log" 2>&1; then
        sed 's/^/  /' "$WORK_DIR/validate.log" >&2 || true
        fail "the merged configuration does not validate; refusing to publish"
    fi
    info "configuration is valid"
fi

PROMOTION_STATE="$(python3 - "$CONFIG" <<'PY'
import sys, yaml
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
except Exception as exc:                       # noqa: BLE001
    sys.stderr.write(f"error: {exc}\n")
    sys.exit(2)
print((cfg.get("promotion_state") or "").strip())
PY
)" || fail "could not read promotion_state from $CONFIG"

info "promotion state: ${PROMOTION_STATE:-<unset>}"
if [ "$PROMOTION_STATE" != "open" ]; then
    # frozen means an RC uprev is in progress: promotions may validate but must
    # not publish.
    fail "promotion_state is '${PROMOTION_STATE:-<unset>}', not 'open'; publication is not permitted"
fi

# --------------------------------------------------------------------------
step "Computing the digest of the merged configuration"

if [ -n "$DIGEST_OVERRIDE" ]; then
    DIGEST="$DIGEST_OVERRIDE"
    info "digest         : $DIGEST (supplied)"
else
    python3 "$HELPER" --config "$CONFIG" tested-set >"$WORK_DIR/expected-from-config.txt" \
        || fail "could not project the config into a tested-set"
    DIGEST="$(python3 "$HELPER" digest "$WORK_DIR/expected-from-config.txt" --quiet)" \
        || fail "could not compute the config digest"
    info "digest         : $DIGEST"
fi
case "$DIGEST" in
    [0-9a-f]*) [ "${#DIGEST}" -eq 64 ] || fail "digest is not 64-hex: $DIGEST" ;;
    *) fail "digest is not lowercase 64-hex: $DIGEST" ;;
esac

MANIFEST_REF="${REF_NS}/${DIGEST}"

# --------------------------------------------------------------------------
step "Looking up the tested candidate"

git init --quiet "$KDIR" || fail "could not create a work repository"
git -C "$KDIR" remote add candidate "$(auth_url "$CANDIDATE_REPO")"

if ! git -C "$KDIR" fetch --quiet --no-tags candidate \
        "+${MANIFEST_REF}:refs/manifest" 2>"$WORK_DIR/fetch.log"; then
    scrub < "$WORK_DIR/fetch.log" | sed 's/^/  /' >&2 || true
    fail "no tested candidate recorded for digest ${DIGEST:0:16}.
  Expected $MANIFEST_REF in $CANDIDATE_REPO.
  Either the config PR CI has not completed a green run for this exact
  configuration, or the manifest was never pushed. Re-run the PR CI for the
  merged config, then re-run publication."
fi

MANIFEST_COMMIT="$(git -C "$KDIR" rev-parse refs/manifest)" \
    || fail "could not resolve the fetched manifest"
info "manifest       : ${MANIFEST_COMMIT:0:12} ($MANIFEST_REF)"

git -C "$KDIR" show "refs/manifest:manifest.json" >"$WORK_DIR/manifest.json" 2>/dev/null \
    || fail "the manifest commit has no manifest.json"
git -C "$KDIR" show "refs/manifest:tested-set.txt" >"$WORK_DIR/tested-set.txt" 2>/dev/null \
    || fail "the manifest commit has no tested-set.txt"

read_field() {   # read_field <key>
    python3 - "$WORK_DIR/manifest.json" "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception as exc:                       # noqa: BLE001
    sys.stderr.write(f"error: unreadable manifest: {exc}\n")
    sys.exit(1)
val = data.get(sys.argv[2], "")
if isinstance(val, list):
    print(",".join(str(v) for v in val))
else:
    print(val)
PY
}

M_DIGEST="$(read_field digest)"       || fail "could not read digest from the manifest"
CANDIDATE_SHA="$(read_field candidate_sha)" || fail "could not read candidate_sha from the manifest"
M_PR="$(read_field pr)"
M_REF="$(read_field candidate_ref)"
M_RUN="$(read_field run_url)"
M_STATUSES="$(read_field proven_statuses)"
M_RECORDED="$(read_field recorded_at)"

info "candidate sha  : ${CANDIDATE_SHA:0:12}"
info "recorded       : ${M_RECORDED:-<unknown>} (PR ${M_PR:-?})"

# The ref name already contains the digest, but a manifest whose body disagrees
# with its own ref would mean the record was assembled wrongly.
[ "$M_DIGEST" = "$DIGEST" ] \
    || fail "manifest digest mismatch
  ref says  $DIGEST
  body says $M_DIGEST"

case "$CANDIDATE_SHA" in
    [0-9a-f]*) [ "${#CANDIDATE_SHA}" -eq 40 ] || fail "manifest candidate_sha is not 40-hex: $CANDIDATE_SHA" ;;
    *) fail "manifest candidate_sha is not lowercase 40-hex: $CANDIDATE_SHA" ;;
esac

# Recompute the digest of the recorded tested-set: this is the tamper check.
python3 "$HELPER" digest "$WORK_DIR/tested-set.txt" >/dev/null 2>&1 \
    || fail "the recorded tested-set is not internally consistent"
RECORDED_DIGEST="$(python3 "$HELPER" digest "$WORK_DIR/tested-set.txt" --quiet)"
[ "$RECORDED_DIGEST" = "$DIGEST" ] \
    || fail "the recorded tested-set does not hash to the requested digest
  expected $DIGEST
  actual   $RECORDED_DIGEST"
info "recorded tested-set hashes to the requested digest"

# Independently confirm the merged config projects to the same inputs, so a
# manifest cannot authorise a tree the merged config does not describe.
if [ -z "$DIGEST_OVERRIDE" ]; then
    if ! bash "$CHECKER" --expected "$WORK_DIR/tested-set.txt" \
            --actual "$WORK_DIR/expected-from-config.txt" --quiet \
            >"$WORK_DIR/compare.log" 2>&1; then
        sed 's/^/  /' "$WORK_DIR/compare.log" >&2 || true
        fail "the merged config does not match the tested set"
    fi
    info "merged config matches the tested set"
fi

# --------------------------------------------------------------------------
step "Fetching the tested commit"

if ! git -C "$KDIR" fetch --quiet --no-tags candidate \
        "+${CANDIDATE_SHA}:refs/tested" 2>"$WORK_DIR/fetch-sha.log"; then
    # The manifest keeps the candidate reachable as its first parent, so this
    # should not happen; fall back to it rather than refusing outright.
    info "direct fetch failed; falling back to the manifest's first parent"
    if ! git -C "$KDIR" rev-parse --verify --quiet "refs/manifest^1" >/dev/null; then
        scrub < "$WORK_DIR/fetch-sha.log" | sed 's/^/  /' >&2 || true
        fail "the tested commit $CANDIDATE_SHA is not available in $CANDIDATE_REPO"
    fi
    git -C "$KDIR" update-ref refs/tested "$(git -C "$KDIR" rev-parse 'refs/manifest^1')"
fi

TESTED_SHA="$(git -C "$KDIR" rev-parse refs/tested)"
[ "$TESTED_SHA" = "$CANDIDATE_SHA" ] \
    || fail "fetched commit does not match the manifest
  manifest $CANDIDATE_SHA
  fetched  $TESTED_SHA"

PARENT="$(git -C "$KDIR" rev-parse --verify --quiet 'refs/manifest^1' || echo "")"
if [ -n "$PARENT" ] && [ "$PARENT" != "$CANDIDATE_SHA" ]; then
    fail "the manifest's first parent is not the recorded candidate
  parent   ${PARENT:0:12}
  recorded ${CANDIDATE_SHA:0:12}"
fi
info "tested commit  : ${TESTED_SHA:0:12} (verified against the manifest)"

# --------------------------------------------------------------------------
step "Verifying the tested commit contains the tested set"

if ! bash "$CHECKER" --expected "$WORK_DIR/tested-set.txt" \
        --kernel-dir "$KDIR" --ref "$TESTED_SHA" \
        >"$WORK_DIR/verify.log" 2>&1; then
    sed 's/^/  /' "$WORK_DIR/verify.log" >&2 || true
    fail "the tested commit does not contain the tested set"
fi
[ "$QUIET" -eq 1 ] || sed 's/^/  /' "$WORK_DIR/verify.log"

# --------------------------------------------------------------------------
step "Checking the gate verdicts on the tested commit"

STATUS_REPORT="not checked"
if [ "$SKIP_STATUS" -eq 1 ]; then
    info "skipped (--skip-status-check)"
elif [ -z "$REQUIRE_STATUSES" ]; then
    info "no statuses required"
    STATUS_REPORT="none required"
elif ! command -v gh >/dev/null 2>&1; then
    fail "gh is required to verify gate statuses; pass --skip-status-check only for tests"
else
    GH_STATUS_JSON="$WORK_DIR/statuses.json"
    if ! GH_TOKEN="${GH_TOKEN:-${GH_PUSH_TOKEN:-}}" gh api \
            "repos/${CANDIDATE_REPO}/commits/${TESTED_SHA}/status" \
            >"$GH_STATUS_JSON" 2>"$WORK_DIR/gh.log"; then
        sed 's/^/  /' "$WORK_DIR/gh.log" >&2 || true
        fail "could not read commit statuses for ${TESTED_SHA:0:12} in $CANDIDATE_REPO"
    fi

    missing=""
    for want in $(printf '%s' "$REQUIRE_STATUSES" | tr ',' ' '); do
        [ -n "$want" ] || continue
        state="$(python3 - "$GH_STATUS_JSON" "$want" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
want = sys.argv[2]
# Newest first, so the current verdict wins over earlier attempts.
for st in data.get("statuses") or []:
    if st.get("context") == want:
        print(st.get("state") or "")
        break
else:
    print("absent")
PY
)"
        info "  $want: $state"
        [ "$state" = "success" ] || missing="${missing:+$missing }$want=$state"
    done

    if [ -n "$missing" ]; then
        fail "required gate checks are not green on ${TESTED_SHA:0:12}: $missing
  The tested commit must carry a green status for every required check.
  Re-run the gates for this candidate, then re-run publication."
    fi
    STATUS_REPORT="$REQUIRE_STATUSES all green"
    info "all required checks are green"
fi

# --------------------------------------------------------------------------
step "Publishing"

git -C "$KDIR" remote add publish "$(auth_url "$PUBLISH_REPO")" 2>/dev/null \
    || git -C "$KDIR" remote set-url publish "$(auth_url "$PUBLISH_REPO")"

OLD_HEAD="$(git -C "$KDIR" ls-remote publish "refs/heads/${PUBLISH_REF}" 2>/dev/null | awk '{print $1; exit}')"
if [ -n "$OLD_HEAD" ]; then
    info "current head   : ${OLD_HEAD:0:12}"
else
    info "current head   : <branch does not exist yet>"
fi

if [ "$OLD_HEAD" = "$TESTED_SHA" ]; then
    info ""
    info "RESULT: PASS - $PUBLISH_REF is already at ${TESTED_SHA:0:12}; nothing to do"
    PUBLISHED="already current"
else
    if [ "$DRY_RUN" -eq 1 ]; then
        info ""
        info "RESULT: PASS (dry run) - would publish ${TESTED_SHA:0:12} to ${PUBLISH_REPO}:${PUBLISH_REF}"
        PUBLISHED="dry run"
    else
        # A token is only needed to reach GitHub over https. A local path or an
        # explicit URL carries its own access.
        case "$PUBLISH_REPO" in
            *://*|/*) ;;
            *) [ -n "${GH_PUSH_TOKEN:-}" ] || fail "GH_PUSH_TOKEN is not set; cannot push to $PUBLISH_REPO" ;;
        esac

        # --force-with-lease against the head observed a moment ago: if anything
        # else moved the branch in between, the push is rejected rather than
        # silently discarding that work.
        if [ -n "$OLD_HEAD" ]; then
            lease="--force-with-lease=refs/heads/${PUBLISH_REF}:${OLD_HEAD}"
        else
            # Nothing to lease against; refuse to create the branch implicitly
            # unless it genuinely does not exist.
            lease="--force-with-lease=refs/heads/${PUBLISH_REF}:"
        fi

        push_rc=0
        git -C "$KDIR" push "$lease" publish \
            "${TESTED_SHA}:refs/heads/${PUBLISH_REF}" 2>&1 | scrub || push_rc=1
        [ "$push_rc" -eq 0 ] || fail "the push was rejected. ${PUBLISH_REF} moved since it was read
  (previous head ${OLD_HEAD:-<none>}). Nothing was published; re-run to retry."

        NEW_HEAD="$(git -C "$KDIR" ls-remote publish "refs/heads/${PUBLISH_REF}" 2>/dev/null | awk '{print $1; exit}')"
        [ "$NEW_HEAD" = "$TESTED_SHA" ] \
            || fail "post-push verification failed: ${PUBLISH_REF} is at ${NEW_HEAD:-<none>}, expected ${TESTED_SHA:0:12}"

        info ""
        info "RESULT: PASS - published ${TESTED_SHA:0:12} to ${PUBLISH_REPO}:${PUBLISH_REF}"
        info "  previous head was ${OLD_HEAD:-<none>} (rollback point)"
        PUBLISHED="published"
    fi
fi

git -C "$KDIR" remote remove publish 2>/dev/null || true
git -C "$KDIR" remote remove candidate 2>/dev/null || true

# --------------------------------------------------------------------------
if [ -n "$OUTPUT" ]; then
    {
        echo "### qcom-next publication"
        echo ""
        echo "| Field | Value |"
        echo "|---|---|"
        echo "| Outcome | \`${PUBLISHED}\` |"
        echo "| Published to | \`${PUBLISH_REPO}\` @ \`${PUBLISH_REF}\` |"
        echo "| Published commit | \`${TESTED_SHA}\` |"
        echo "| Previous head | \`${OLD_HEAD:-none}\` |"
        echo "| Config digest | \`${DIGEST}\` |"
        echo "| Candidate repo | \`${CANDIDATE_REPO}\` |"
        echo "| Candidate ref | \`${M_REF:-none}\` |"
        echo "| Source PR | \`${M_PR:-unknown}\` |"
        echo "| Gate checks | \`${STATUS_REPORT}\` |"
        echo "| Proven at record time | \`${M_STATUSES:-none}\` |"
        echo "| Recorded at | \`${M_RECORDED:-unknown}\` |"
        [ -n "$M_RUN" ] && echo "| Testing run | ${M_RUN} |"
        echo ""
        if [ "$PUBLISHED" = "published" ]; then
            echo "Rollback: \`git push --force-with-lease=refs/heads/${PUBLISH_REF}:${TESTED_SHA} \\"
            echo "  https://github.com/${PUBLISH_REPO} ${OLD_HEAD:-<none>}:refs/heads/${PUBLISH_REF}\`"
        fi
    } > "$OUTPUT"
    info "summary written to $OUTPUT"
fi

exit 0
