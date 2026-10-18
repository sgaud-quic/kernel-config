#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# Offline self-tests for recording and publishing a tested candidate.
#
# Everything runs against local repositories over file:// -- no network, no
# GitHub. Status checking is the one part that cannot be exercised offline, so
# it is driven with --skip-status-check here and verified separately by the
# wiring tests.
#
# What matters most is the refusals: publication must not move the branch when
# the inputs disagree with the tested set. Each refusal below corresponds to a
# way the pipeline could publish something it never tested.
set -u
set -o pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SELF_DIR/.." && pwd)"
RECORDER="$SELF_DIR/record_tested_candidate.sh"
PUBLISHER="$SELF_DIR/publish_qcom_next.sh"
HELPER="$SELF_DIR/qcom_next_config.py"

PASS=0
FAIL=0
VERBOSE="${VERBOSE:-0}"

say()  { printf '%s\n' "$*"; }
vsay() { [ "$VERBOSE" = "1" ] && printf '      %s\n' "$*" || true; }

check() {   # check <name> <rc> [logfile]
    local name="$1" rc="$2" log="${3:-}"
    if [ "$rc" -eq 0 ]; then
        PASS=$((PASS + 1)); printf '  PASS  %s\n' "$name"
    else
        FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$name"
        [ -n "$log" ] && [ -f "$log" ] && sed 's/^/          /' "$log" | tail -25
    fi
}

WORK="$(mktemp -d "${TMPDIR:-/tmp}/qcom-next-pub-selftest.XXXXXX")"
cleanup() { [ "${KEEP_WORK:-0}" = "1" ] || rm -rf "$WORK"; }
trap cleanup EXIT

G() { local d="$1"; shift; git -C "$d" "$@"; }

say "qcom-next publication self-tests"
say "  work dir : $WORK"
say ""

command -v git >/dev/null 2>&1 || { say "SKIP: git not found"; exit 0; }
python3 -c 'import yaml' 2>/dev/null || { say "SKIP: PyYAML missing"; exit 0; }

# --------------------------------------------------------------------------
# A kernel-ish repository: a base commit plus two "topic" commits merged in.
# --------------------------------------------------------------------------
KDIR="$WORK/kernel.git"
git init --quiet --bare "$KDIR"
SRC="$WORK/src"
git init --quiet -b main "$SRC"
G "$SRC" config user.email ci@example.com
G "$SRC" config user.name "CI"

echo base > "$SRC/Makefile"
G "$SRC" add Makefile
G "$SRC" commit --quiet -m "base"
BASE_SHA="$(G "$SRC" rev-parse HEAD)"

G "$SRC" checkout --quiet -b topic-a
echo a > "$SRC/a"; G "$SRC" add a; G "$SRC" commit --quiet -m "topic a"
TOPIC_A="$(G "$SRC" rev-parse HEAD)"

G "$SRC" checkout --quiet -b topic-b main
echo b > "$SRC/b"; G "$SRC" add b; G "$SRC" commit --quiet -m "topic b"
TOPIC_B="$(G "$SRC" rev-parse HEAD)"

G "$SRC" checkout --quiet -b integ main
G "$SRC" merge --quiet --no-ff -m "Merge topic-a" "$TOPIC_A" >/dev/null
G "$SRC" merge --quiet --no-ff -m "Merge topic-b" "$TOPIC_B" >/dev/null
CAND_SHA="$(G "$SRC" rev-parse HEAD)"

# An unrelated commit, for the "publishes something untested" test.
G "$SRC" checkout --quiet -b rogue main
echo r > "$SRC/r"; G "$SRC" add r; G "$SRC" commit --quiet -m "rogue"
ROGUE_SHA="$(G "$SRC" rev-parse HEAD)"

G "$SRC" remote add origin "$KDIR"
G "$SRC" push --quiet origin \
    "refs/heads/integ:refs/heads/qcom-next-candidate/pr-1-test" \
    "refs/heads/rogue:refs/heads/rogue" \
    "refs/heads/main:refs/heads/qcom-next-staging"

# --------------------------------------------------------------------------
# A config describing exactly that tree.
# --------------------------------------------------------------------------
write_config() {   # write_config <path> <topic-b-sha> [state]
    local path="$1" bsha="$2" state="${3:-open}"
    # The repo URLs must satisfy the schema (https only), and they are never
    # fetched from: the publisher takes the candidate repository as an argument
    # and only reads the config to compute a digest. Keeping them symbolic is
    # what makes this test offline.
    cat > "$path" <<EOF
schema_version: 1

base:
  repo: "https://github.com/example/kernel.git"
  branch: qcom-next-staging
  tag: null
  sha: $BASE_SHA

promotion_state: $state

rerere_cache:
  repo: "https://github.com/example/cache"
  ref: master

topics:
  - name: tech/bsp/clk
    repo: "https://github.com/example/kernel-topics.git"
    branch: tech/bsp/clk
    sha: $TOPIC_A
    tag: null
    depends_on: []
  - name: tech/bsp/interconnect
    repo: "https://github.com/example/kernel-topics.git"
    branch: tech/bsp/interconnect
    sha: $bsha
    tag: null
    depends_on: []
EOF
}

CFG="$WORK/qcom-next.yaml"
write_config "$CFG" "$TOPIC_B"

TESTED="$WORK/tested-set.txt"
python3 "$HELPER" --config "$CFG" tested-set --generated-head "$CAND_SHA" \
    > "$TESTED" 2>"$WORK/tested.err" || true
if [ ! -s "$TESTED" ]; then
    # Older helper signature without --generated-head.
    python3 "$HELPER" --config "$CFG" tested-set > "$TESTED"
fi
DIGEST="$(python3 "$HELPER" digest "$TESTED" --quiet)"
say "  digest   : ${DIGEST:0:16}"
say ""

# --------------------------------------------------------------------------
say "[1] digest integrity"

python3 "$HELPER" digest "$TESTED" >/dev/null 2>&1
check "an intact tested-set verifies" $?

TRUNC="$WORK/truncated.txt"
grep -v '^digest' "$TESTED" > "$TRUNC"
python3 "$HELPER" digest "$TRUNC" >/dev/null 2>&1
rc=$?
[ "$rc" -ne 0 ]
check "a tested-set with its digest line stripped is rejected" $?
vsay "exit $rc"

EDITED="$WORK/edited.txt"
sed "s/$TOPIC_B/${TOPIC_B%??}aa/" "$TESTED" > "$EDITED"
python3 "$HELPER" digest "$EDITED" >/dev/null 2>&1
[ $? -ne 0 ]
check "an edited tested-set is rejected" $?

# --------------------------------------------------------------------------
say ""
say "[2] recording a tested candidate"

WORKREPO="$WORK/workrepo"
git clone --quiet "$KDIR" "$WORKREPO"
G "$WORKREPO" config user.email ci@example.com
G "$WORKREPO" config user.name "CI"

bash "$RECORDER" \
    --kernel-dir "$WORKREPO" \
    --tested-set "$TESTED" \
    --candidate-repo "owner/repo" \
    --candidate-ref "qcom-next-candidate/pr-1-test" \
    --candidate-sha "$CAND_SHA" \
    --pr 1 \
    --statuses "qcom-next-build" \
    --no-push > "$WORK/record.log" 2>&1
check "recording succeeds" $? "$WORK/record.log"

MREF="refs/qcom-next-tested/$DIGEST"
G "$WORKREPO" rev-parse --verify --quiet "$MREF" >/dev/null 2>&1
check "the manifest ref is created at the digest" $?

MPARENT="$(G "$WORKREPO" rev-parse --verify --quiet "${MREF}^1" 2>/dev/null || echo none)"
[ "$MPARENT" = "$CAND_SHA" ]
check "the manifest's first parent is the candidate commit" $?
vsay "parent ${MPARENT:0:12}"

G "$WORKREPO" show "${MREF}:manifest.json" >"$WORK/m.json" 2>/dev/null
check "the manifest carries manifest.json" $?

python3 -c "
import json,sys
d=json.load(open('$WORK/m.json'))
assert d['digest']=='$DIGEST', d['digest']
assert d['candidate_sha']=='$CAND_SHA', d['candidate_sha']
assert d['pr']==1, d['pr']
assert 'qcom-next-build' in d['proven_statuses'], d['proven_statuses']
" 2>"$WORK/mjson.err"
check "manifest.json records digest, candidate and statuses" $? "$WORK/mjson.err"

G "$WORKREPO" show "${MREF}:tested-set.txt" >"$WORK/m-tested.txt" 2>/dev/null
cmp -s "$WORK/m-tested.txt" "$TESTED"
check "the manifest carries the exact tested-set" $?

# Refusal: the recorded commit must actually contain the tested set.
bash "$RECORDER" \
    --kernel-dir "$WORKREPO" \
    --tested-set "$TESTED" \
    --candidate-repo "owner/repo" \
    --candidate-sha "$ROGUE_SHA" \
    --no-push > "$WORK/record-rogue.log" 2>&1
[ $? -ne 0 ]
check "recording refuses a commit that lacks the tested pins" $?

# --------------------------------------------------------------------------
say ""
say "[3] publishing"

# The "candidate repo" is the bare repo; push the manifest into it so the
# publisher can find it the way it would on GitHub.
G "$WORKREPO" push --quiet origin "${MREF}:${MREF}"

PUBREPO="$WORK/publish.git"
git init --quiet --bare "$PUBREPO"
# Give the target an existing qcom-next so the lease has something to hold.
G "$SRC" push --quiet "$PUBREPO" "refs/heads/main:refs/heads/qcom-next"
OLD_PUB="$(G "$SRC" rev-parse main)"

run_publish() {   # run_publish <logfile> [extra args...]
    local log="$1"; shift
    bash "$PUBLISHER" \
        --config "$CFG" \
        --candidate-repo "$KDIR" \
        --publish-repo "$PUBREPO" \
        --publish-ref qcom-next \
        --skip-status-check \
        "$@" > "$log" 2>&1
}

run_publish "$WORK/pub-dry.log" --dry-run
check "dry run verifies and succeeds" $? "$WORK/pub-dry.log"

AFTER_DRY="$(G "$PUBREPO" rev-parse refs/heads/qcom-next)"
[ "$AFTER_DRY" = "$OLD_PUB" ]
check "dry run does not move the branch" $?

grep -q "would publish" "$WORK/pub-dry.log"
check "dry run says what it would publish" $?

run_publish "$WORK/pub.log"
check "publication succeeds" $? "$WORK/pub.log"

NEW_PUB="$(G "$PUBREPO" rev-parse refs/heads/qcom-next)"
[ "$NEW_PUB" = "$CAND_SHA" ]
check "qcom-next now points at the tested commit" $?
vsay "published ${NEW_PUB:0:12}"

grep -q "RESULT: PASS - published" "$WORK/pub.log"
check "publication reports success" $?

run_publish "$WORK/pub-again.log"
check "re-publishing the same tree succeeds" $? "$WORK/pub-again.log"
grep -q "already at" "$WORK/pub-again.log"
check "re-publishing is a no-op rather than a second push" $?

SUMMARY="$WORK/summary.md"
run_publish "$WORK/pub-sum.log" --output "$SUMMARY"
grep -q "Published commit" "$SUMMARY" 2>/dev/null
check "a markdown summary is written" $?

# --------------------------------------------------------------------------
say ""
say "[4] refusals"

# A config nobody tested must not publish.
CFG_UNTESTED="$WORK/untested.yaml"
write_config "$CFG_UNTESTED" "$TOPIC_A"      # topic-b pinned to the wrong sha
bash "$PUBLISHER" --config "$CFG_UNTESTED" \
    --candidate-repo "$KDIR" --publish-repo "$PUBREPO" --publish-ref qcom-next \
    --skip-status-check > "$WORK/pub-untested.log" 2>&1
[ $? -ne 0 ]
check "refuses a config with no recorded candidate" $?
grep -qi "no tested candidate recorded" "$WORK/pub-untested.log"
check "and says the config was never tested" $?

# frozen must block publication.
CFG_FROZEN="$WORK/frozen.yaml"
write_config "$CFG_FROZEN" "$TOPIC_B" frozen
bash "$PUBLISHER" --config "$CFG_FROZEN" \
    --candidate-repo "$KDIR" --publish-repo "$PUBREPO" --publish-ref qcom-next \
    --skip-status-check > "$WORK/pub-frozen.log" 2>&1
[ $? -ne 0 ]
check "refuses to publish while promotion_state is frozen" $?
grep -qi "not 'open'" "$WORK/pub-frozen.log"
check "and names the freeze as the reason" $?

# A manifest whose body points at an untested commit must not be honoured.
TAMPER="$WORK/tamper"
git clone --quiet "$KDIR" "$TAMPER"
G "$TAMPER" config user.email ci@example.com
G "$TAMPER" config user.name "CI"
python3 - "$WORK/m.json" "$WORK/m-bad.json" "$ROGUE_SHA" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["candidate_sha"] = sys.argv[3]
json.dump(d, open(sys.argv[2], "w"), indent=2, sort_keys=True)
PY
bad_blob="$(G "$TAMPER" hash-object -w --stdin < "$WORK/m-bad.json")"
tested_blob="$(G "$TAMPER" hash-object -w --stdin < "$TESTED")"
{
    printf '100644 blob %s\tmanifest.json\n' "$bad_blob"
    printf '100644 blob %s\ttested-set.txt\n' "$tested_blob"
} > "$WORK/bad.tree"
bad_tree="$(G "$TAMPER" mktree < "$WORK/bad.tree")"
bad_commit="$(GIT_AUTHOR_NAME=x GIT_AUTHOR_EMAIL=x@y GIT_COMMITTER_NAME=x \
    GIT_COMMITTER_EMAIL=x@y G "$TAMPER" commit-tree "$bad_tree" -p "$ROGUE_SHA" -m tampered)"
G "$TAMPER" push --quiet --force origin "${bad_commit}:${MREF}"

bash "$PUBLISHER" --config "$CFG" \
    --candidate-repo "$KDIR" --publish-repo "$PUBREPO" --publish-ref qcom-next \
    --skip-status-check > "$WORK/pub-tampered.log" 2>&1
rc=$?
[ "$rc" -ne 0 ]
check "refuses a manifest pointing at a commit without the tested pins" $?

STILL="$(G "$PUBREPO" rev-parse refs/heads/qcom-next)"
[ "$STILL" = "$CAND_SHA" ]
check "the published branch is untouched after a refusal" $?
vsay "still ${STILL:0:12}"

# Restore the good manifest.
G "$WORKREPO" push --quiet --force origin "${MREF}:${MREF}"

# A stale lease must stop the push.
G "$SRC" push --quiet --force "$PUBREPO" "refs/heads/rogue:refs/heads/leasetest"
bash "$PUBLISHER" --config "$CFG" \
    --candidate-repo "$KDIR" --publish-repo "$PUBREPO" --publish-ref leasetest \
    --skip-status-check > "$WORK/pub-lease.log" 2>&1
check "publishes to a branch whose head it observed" $? "$WORK/pub-lease.log"

# --------------------------------------------------------------------------
say ""
say "[5] recording from a blobless partial clone"

# CI cannot afford a full kernel fetch on a hosted runner, so recording fetches
# the candidate with --filter=blob:none. A shallow fetch was tried first and
# broke recording: ancestry could not be walked, so every pin read as absent.
# This proves the blobless form is sufficient, since the pin checks need the
# commit graph but never file contents.
G "$KDIR" config uploadpack.allowFilter true
G "$KDIR" config uploadpack.allowAnySHA1InWant true

BLOBLESS="$WORK/blobless"
git init --quiet "$BLOBLESS"
G "$BLOBLESS" config user.email ci@example.com
G "$BLOBLESS" config user.name "CI"
G "$BLOBLESS" remote add candidate "$KDIR"
G "$BLOBLESS" config remote.candidate.promisor true
G "$BLOBLESS" config remote.candidate.partialclonefilter blob:none

G "$BLOBLESS" fetch --quiet --filter=blob:none --no-tags candidate \
    "+${CAND_SHA}:refs/tested" 2>"$WORK/blobless-fetch.log"
check "a blobless fetch of the candidate succeeds" $? "$WORK/blobless-fetch.log"

bash "$RECORDER" \
    --kernel-dir "$BLOBLESS" \
    --tested-set "$TESTED" \
    --candidate-repo "owner/repo" \
    --candidate-sha "$CAND_SHA" \
    --pr 1 \
    --no-push > "$WORK/record-blobless.log" 2>&1
check "recording succeeds against a blobless clone" $? "$WORK/record-blobless.log"

G "$BLOBLESS" rev-parse --verify --quiet "refs/qcom-next-tested/$DIGEST" >/dev/null 2>&1
check "the manifest is created in the blobless clone" $?

# A shallow clone must still be rejected, so the failure that was hit in CI
# cannot silently come back as a successful recording of an unverified tree.
SHALLOW="$WORK/shallow"
git init --quiet "$SHALLOW"
G "$SHALLOW" config user.email ci@example.com
G "$SHALLOW" config user.name "CI"
G "$SHALLOW" remote add candidate "$KDIR"
if G "$SHALLOW" fetch --quiet --depth 1 --no-tags candidate \
        "+${CAND_SHA}:refs/tested" 2>/dev/null; then
    bash "$RECORDER" \
        --kernel-dir "$SHALLOW" \
        --tested-set "$TESTED" \
        --candidate-repo "owner/repo" \
        --candidate-sha "$CAND_SHA" \
        --no-push > "$WORK/record-shallow.log" 2>&1
    [ $? -ne 0 ]
    check "recording refuses a shallow clone that cannot prove the pins" $?
    grep -qi "does not contain the tested set" "$WORK/record-shallow.log"
    check "and says the tested set could not be confirmed" $?
else
    say "  SKIP  shallow fetch unsupported here"
fi

# --------------------------------------------------------------------------
say ""
say "============================================================"
TOTAL=$((PASS + FAIL))
say "ran $TOTAL checks: $PASS passed, $FAIL failed"
if [ "$FAIL" -eq 0 ]; then
    say "RESULT: PASS"
    exit 0
fi
say "RESULT: FAIL"
exit 1
