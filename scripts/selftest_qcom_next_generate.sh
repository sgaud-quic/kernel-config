#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# Offline self-tests for SHA-pinned candidate tree generation.
#
# Builds a miniature kernel tree and topic repository on local disk, then drives
# the real automerge ci-merge through generate_qcom_next_tree.sh. No network and
# no access to the production repositories is required, so this runs in CI and
# on a developer machine.
#
# The property under test is the Phase 2 exit criterion: the generated tree is a
# function of the config, not of live branch tips. The decisive cases are
# "advance every branch tip and regenerate" and "generate twice", both of which
# must leave the consumed content unchanged.
#
# Usage: scripts/selftest_qcom_next_generate.sh [-v]
# Exit 0 all tests passed, 1 a test failed, 2 prerequisite missing.
set -u
set -o pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SELF_DIR/.." && pwd)"
GENERATE="$SELF_DIR/generate_qcom_next_tree.sh"
CHECKER="$SELF_DIR/check_tested_tree_matches.sh"
HELPER="$SELF_DIR/qcom_next_config.py"

VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1

PASS=0
FAIL=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/qcom-next-gen-selftest.XXXXXX")"

cleanup() { [ "${KEEP_WORK:-0}" = "1" ] || rm -rf "$WORK"; }
trap cleanup EXIT

say()  { printf '%s\n' "$*"; }
vsay() { [ "$VERBOSE" -eq 1 ] && printf '      %s\n' "$*" || true; }

ok()   { printf '  ok    %s\n' "$*"; PASS=$((PASS + 1)); }
no()   { printf '  FAIL  %s\n' "$*"; FAIL=$((FAIL + 1)); }

check() {  # check <label> <condition-rc> [detail-file]
    if [ "$2" -eq 0 ]; then ok "$1"; else
        no "$1"
        # Dump the whole log. The fixtures are small, and an excerpt hid the
        # real cause behind the banner for a full CI cycle: the first 25 lines
        # are all preamble, and the failure is always at the end.
        if [ -n "${3:-}" ] && [ -f "$3" ]; then
            sed 's/^/          /' "$3"
        fi
    fi
}

# --------------------------------------------------------------------------
say "qcom-next candidate generation self-tests"
say "  generate : $GENERATE"
say "  checker  : $CHECKER"
say "  automerge: ${AUTOMERGE_DIR:-<auto-detect>}"
say "  workdir  : $WORK"

command -v git >/dev/null 2>&1     || { say "error: git missing"; exit 2; }
command -v python3 >/dev/null 2>&1 || { say "error: python3 missing"; exit 2; }
python3 -c 'import yaml' 2>/dev/null || { say "error: PyYAML missing"; exit 2; }

# Locate ci-merge: env override, sibling checkout, or two levels up.
if [ -z "${AUTOMERGE_DIR:-}" ]; then
    for cand in "$REPO_DIR/../automerge" "$REPO_DIR/../../automerge"; do
        if [ -f "$cand/ci-merge" ]; then
            AUTOMERGE_DIR="$(cd "$cand" && pwd)"
            break
        fi
    done
fi
if [ -z "${AUTOMERGE_DIR:-}" ] || [ ! -f "$AUTOMERGE_DIR/ci-merge" ]; then
    say ""
    say "SKIP: ci-merge not found. Set AUTOMERGE_DIR to an automerge checkout."
    exit 0
fi
# Auto-detection above absolutises the path, but an explicitly supplied one may
# be relative. The generator runs from throwaway work directories, so a relative
# path stops resolving there and ci-merge silently fails to run, surfacing much
# later as "ci-merge did not create the integration branch". The check above
# cannot catch it because the working directory is still correct at this point.
AUTOMERGE_DIR="$(cd "$AUTOMERGE_DIR" && pwd)"
say "  ci-merge : $AUTOMERGE_DIR/ci-merge"

BASH_MAJOR="$(bash --version | sed -n '1s/.*version \([0-9]*\).*/\1/p')"
say "  bash     : ${BASH_MAJOR:-unknown}"

G() { git -C "$1" "${@:2}"; }

# --------------------------------------------------------------------------
# Fixture: an upstream repo holding both the baseline branch and the topics.
# One repo is enough and mirrors production, where every topic lives in
# kernel-topics.
# --------------------------------------------------------------------------
say ""
say "building fixture repositories"

UPSTREAM="$WORK/upstream.git"
mkdir -p "$UPSTREAM"
G "$UPSTREAM" init -q
G "$UPSTREAM" config user.email "fixture@example.com"
G "$UPSTREAM" config user.name  "fixture"
G "$UPSTREAM" config commit.gpgsign false

# Minimal kernel-ish tree so the generator's sanity check passes.
mkdir -p "$UPSTREAM/scripts" "$UPSTREAM/drivers/clk" "$UPSTREAM/drivers/gpu" "$UPSTREAM/drivers/net"
cat > "$UPSTREAM/Makefile" <<'EOF'
VERSION = 7
PATCHLEVEL = 3
SUBLEVEL = 0
EXTRAVERSION = -rc6
NAME = Fixture Kernel
EOF
echo "exit 0" > "$UPSTREAM/scripts/checkpatch.pl"
echo "baseline" > "$UPSTREAM/README.fixture"
G "$UPSTREAM" add -A
G "$UPSTREAM" commit -q -m "baseline: initial kernel tree"
G "$UPSTREAM" branch -M main

# A second baseline commit, so we can pin an older base and prove it is honoured.
echo "baseline rc6 extra" >> "$UPSTREAM/README.fixture"
G "$UPSTREAM" add -A
G "$UPSTREAM" commit -q -m "baseline: second commit"
BASE_OLD="$(G "$UPSTREAM" rev-parse HEAD)"
G "$UPSTREAM" tag "v7.3-rc6"

# Topic branches, each touching a distinct file so they merge cleanly.
make_topic() {   # make_topic <branch> <dir> <file>
    local branch="$1" dir="$2" file="$3"
    G "$UPSTREAM" checkout -q -b "$branch" "$BASE_OLD"
    mkdir -p "$UPSTREAM/$dir"
    echo "$branch v1" > "$UPSTREAM/$dir/$file"
    G "$UPSTREAM" add -A
    G "$UPSTREAM" commit -q -m "$branch: add $file"
    echo "$branch v2" >> "$UPSTREAM/$dir/$file"
    G "$UPSTREAM" add -A
    G "$UPSTREAM" commit -q -m "$branch: extend $file"
}

make_topic "tech/bsp/clk"  "drivers/clk" "clk-fixture.c"
make_topic "tech/mm/drm"   "drivers/gpu" "drm-fixture.c"
make_topic "tech/net/eth"  "drivers/net" "eth-fixture.c"

CLK_V1="$(G "$UPSTREAM" rev-parse 'tech/bsp/clk~1')"
CLK_V2="$(G "$UPSTREAM" rev-parse 'tech/bsp/clk')"
DRM_V2="$(G "$UPSTREAM" rev-parse 'tech/mm/drm')"
ETH_V2="$(G "$UPSTREAM" rev-parse 'tech/net/eth')"
G "$UPSTREAM" checkout -q main

# Tag the pinned commits, as the real flow requires (FR-INT-01).
G "$UPSTREAM" tag "tech/bsp/clk-7.3-rc6-20261017"   "$CLK_V1"
G "$UPSTREAM" tag "tech/mm/drm-7.3-rc6-20261017"    "$DRM_V2"
G "$UPSTREAM" tag "tech/net/eth-7.3-rc6-20261017"   "$ETH_V2"

vsay "base    $BASE_OLD"
vsay "clk v1  $CLK_V1  (the pin: deliberately NOT the branch tip)"
vsay "clk v2  $CLK_V2  (the tip, which must be ignored)"

# A rerere cache repo, since ci-merge clones whatever -c points at.
RERERE="$WORK/rerere.git"
mkdir -p "$RERERE"
G "$RERERE" init -q
G "$RERERE" config user.email "fixture@example.com"
G "$RERERE" config user.name  "fixture"
G "$RERERE" config commit.gpgsign false
mkdir -p "$RERERE/rr-cache"
echo "placeholder" > "$RERERE/rr-cache/.gitkeep"
G "$RERERE" add -A
G "$RERERE" commit -q -m "rr-cache: initial"
G "$RERERE" branch -M master

say "  upstream : $UPSTREAM"
say "  rerere   : $RERERE"

# --------------------------------------------------------------------------
write_config() {   # write_config <path> <clk-sha> [promotion_state]
    local path="$1" clk="$2" state="${3:-open}"
    cat > "$path" <<EOF
schema_version: 1

base:
  repo: "file://$UPSTREAM"
  branch: main
  tag: v7.3-rc6
  sha: $BASE_OLD

promotion_state: $state

rerere_cache:
  repo: "file://$RERERE"
  ref: master

topics:
  - name: tech/bsp/clk
    repo: "file://$UPSTREAM"
    branch: tech/bsp/clk
    sha: $clk
    tag: tech/bsp/clk-7.3-rc6-20261017
    depends_on: []
  - name: tech/mm/drm
    repo: "file://$UPSTREAM"
    branch: tech/mm/drm
    sha: $DRM_V2
    tag: tech/mm/drm-7.3-rc6-20261017
    depends_on: []
  - name: tech/net/eth
    repo: "file://$UPSTREAM"
    branch: tech/net/eth
    sha: $ETH_V2
    tag: tech/net/eth-7.3-rc6-20261017
    depends_on: []
EOF
}

fresh_kernel() {   # fresh_kernel <dir>
    rm -rf "$1"
    git clone -q "file://$UPSTREAM" "$1" 2>/dev/null
    G "$1" config user.email "ci@example.com"
    G "$1" config user.name  "ci"
    G "$1" config commit.gpgsign false
    G "$1" checkout -q main
}

run_generate() {   # run_generate <config> <kernel-dir> <out-dir> <log> [extra...]
    local cfg="$1" kdir="$2" out="$3" log="$4"; shift 4
    bash "$GENERATE" \
        --config "$cfg" \
        --kernel-dir "$kdir" \
        --automerge "$AUTOMERGE_DIR" \
        --out-dir "$out" \
        --integ-branch qcom-next \
        "$@" > "$log" 2>&1
}

# ==========================================================================
say ""
say "[1] config projection"

CFG="$WORK/qcom-next.yaml"
write_config "$CFG" "$CLK_V1"

python3 "$HELPER" --config "$CFG" ref-clashes >/dev/null 2>&1
check "no ref name collisions in the fixture config" $?

python3 "$HELPER" --config "$CFG" merge-conf --url-override "file:///k" \
    > "$WORK/mc.txt" 2>/dev/null
rows=$(grep -vc '^#' "$WORK/mc.txt" 2>/dev/null || echo 0)
[ "$(grep -c '^baseline' "$WORK/mc.txt")" -eq 1 ]
check "merge-conf emits exactly one baseline row" $?

grep -q '^tech/bsp/clk  *file:///k  *tech/bsp/clk' "$WORK/mc.txt"
check "merge-conf rewrites topic URLs to the override" $?

python3 "$HELPER" --config "$CFG" pin-refs > "$WORK/pins.txt" 2>/dev/null
grep -qx "refs/heads/override/tech/bsp/clk	$CLK_V1" "$WORK/pins.txt"
check "pin-refs points override/tech/bsp/clk at the pinned sha" $?

grep -qx "refs/heads/main	$BASE_OLD" "$WORK/pins.txt"
check "pin-refs points the baseline branch at base.sha" $?

# ==========================================================================
say ""
say "[2] generation pins SHAs rather than branch tips"

KDIR="$WORK/kernel-a"
OUT="$WORK/out-a"
LOG="$WORK/gen-a.log"
fresh_kernel "$KDIR"
run_generate "$CFG" "$KDIR" "$OUT" "$LOG"
GEN_RC=$?
check "generation succeeds" "$GEN_RC" "$LOG"

if [ "$GEN_RC" -eq 0 ]; then
    grep -q 'RESULT: PASS' "$LOG"
    check "generation reports PASS" $?

    G "$KDIR" rev-parse --verify --quiet refs/heads/qcom-next >/dev/null
    check "integration branch created" $?

    HEAD_SHA="$(G "$KDIR" rev-parse refs/heads/qcom-next)"

    G "$KDIR" merge-base --is-ancestor "$CLK_V1" "$HEAD_SHA"
    check "pinned clk commit (v1) is in the tree" $?

    if G "$KDIR" merge-base --is-ancestor "$CLK_V2" "$HEAD_SHA" 2>/dev/null; then
        no "branch tip (clk v2) must NOT be in the tree - pinning failed"
    else
        ok "branch tip (clk v2) correctly excluded"
    fi

    G "$KDIR" merge-base --is-ancestor "$BASE_OLD" "$HEAD_SHA"
    check "pinned base commit is an ancestor" $?

    G "$KDIR" merge-base --is-ancestor "$DRM_V2" "$HEAD_SHA"
    check "pinned drm commit is in the tree" $?
    G "$KDIR" merge-base --is-ancestor "$ETH_V2" "$HEAD_SHA"
    check "pinned eth commit is in the tree" $?

    # The content of the pinned commit, not the tip, must be on disk.
    if [ -f "$KDIR/drivers/clk/clk-fixture.c" ]; then
        if grep -q "v2" "$KDIR/drivers/clk/clk-fixture.c" 2>/dev/null; then
            no "worktree contains clk v2 content; the tip leaked in"
        else
            ok "worktree contains the pinned clk v1 content only"
        fi
    else
        no "clk fixture file missing from the generated worktree"
    fi

    for f in merge.conf merge.log tested-set.txt topic-shas.tsv summary.md; do
        [ -s "$OUT/$f" ]
        check "artifact written: $f" $?
    done

    [ "$(awk -F'\t' '$4=="merged"' "$OUT/topic-shas.tsv" | grep -c .)" -eq 3 ]
    check "topic-shas.tsv records 3 merged topics" $?

    grep -q "^base	$BASE_OLD" "$OUT/tested-set.txt"
    check "tested-set records the pinned base" $?
    grep -q "^topic	tech/bsp/clk	$CLK_V1" "$OUT/tested-set.txt"
    check "tested-set records the pinned clk sha" $?
fi

# ==========================================================================
say ""
say "[3] determinism"

KDIR2="$WORK/kernel-b"
OUT2="$WORK/out-b"
fresh_kernel "$KDIR2"
run_generate "$CFG" "$KDIR2" "$OUT2" "$WORK/gen-b.log"
check "second generation from the same config succeeds" $? "$WORK/gen-b.log"

D1="$(python3 "$HELPER" digest "$OUT/tested-set.txt" --quiet 2>/dev/null || echo x)"
D2="$(python3 "$HELPER" digest "$OUT2/tested-set.txt" --quiet 2>/dev/null || echo y)"
[ "$D1" = "$D2" ] && [ "$D1" != "x" ]
check "two runs produce an identical tested-set digest" $?
vsay "digest $D1"

bash "$CHECKER" --expected "$OUT/tested-set.txt" --actual "$OUT2/tested-set.txt" \
    > "$WORK/chk-ab.log" 2>&1
check "checker accepts the two equivalent runs" $? "$WORK/chk-ab.log"

bash "$CHECKER" --expected "$OUT/tested-set.txt" --kernel-dir "$KDIR2" --ref qcom-next \
    > "$WORK/chk-tree.log" 2>&1
check "checker verifies run A's tested set against run B's tree" $? "$WORK/chk-tree.log"

# ==========================================================================
say ""
say "[4] moving branch tips must not change the generated tree"

# The decisive regression test for this phase: advance every topic tip and the
# baseline tip, regenerate from the unchanged config, and require the consumed
# content to be identical.
G "$UPSTREAM" checkout -q "tech/bsp/clk"
echo "clk v3 upstream churn" >> "$UPSTREAM/drivers/clk/clk-fixture.c"
G "$UPSTREAM" add -A
G "$UPSTREAM" commit -q -m "tech/bsp/clk: post-pin churn"
G "$UPSTREAM" checkout -q main
echo "baseline churn" >> "$UPSTREAM/README.fixture"
G "$UPSTREAM" add -A
G "$UPSTREAM" commit -q -m "baseline: post-pin churn"
BASE_NEW="$(G "$UPSTREAM" rev-parse main)"
vsay "baseline moved $BASE_OLD -> $BASE_NEW"

KDIR3="$WORK/kernel-c"
OUT3="$WORK/out-c"
fresh_kernel "$KDIR3"
run_generate "$CFG" "$KDIR3" "$OUT3" "$WORK/gen-c.log"
check "generation succeeds after upstream tips moved" $? "$WORK/gen-c.log"

D3="$(python3 "$HELPER" digest "$OUT3/tested-set.txt" --quiet 2>/dev/null || echo z)"
[ "$D1" = "$D3" ]
check "digest unchanged despite moved tips" $?

HEAD3="$(G "$KDIR3" rev-parse refs/heads/qcom-next 2>/dev/null || echo none)"
if [ "$HEAD3" != "none" ]; then
    if G "$KDIR3" merge-base --is-ancestor "$BASE_NEW" "$HEAD3" 2>/dev/null; then
        no "new baseline tip leaked into the tree; base pinning failed"
    else
        ok "new baseline tip excluded; base.sha honoured"
    fi
    if G "$KDIR3" merge-base --is-ancestor "$CLK_V2" "$HEAD3" 2>/dev/null; then
        no "clk churn leaked into the tree; topic pinning failed"
    else
        ok "clk churn excluded; topic sha honoured"
    fi
fi

bash "$CHECKER" --expected "$OUT/tested-set.txt" --kernel-dir "$KDIR3" --ref qcom-next \
    > "$WORK/chk-c.log" 2>&1
check "checker accepts the post-churn tree" $? "$WORK/chk-c.log"

# ==========================================================================
say ""
say "[5] promoting a pin changes the tree"

CFG2="$WORK/qcom-next-promoted.yaml"
write_config "$CFG2" "$CLK_V2"
KDIR4="$WORK/kernel-d"
OUT4="$WORK/out-d"
fresh_kernel "$KDIR4"
run_generate "$CFG2" "$KDIR4" "$OUT4" "$WORK/gen-d.log"
check "generation succeeds with the promoted clk sha" $? "$WORK/gen-d.log"

D4="$(python3 "$HELPER" digest "$OUT4/tested-set.txt" --quiet 2>/dev/null || echo w)"
[ "$D1" != "$D4" ]
check "promoting a sha changes the digest" $?

HEAD4="$(G "$KDIR4" rev-parse refs/heads/qcom-next 2>/dev/null || echo none)"
if [ "$HEAD4" != "none" ]; then
    G "$KDIR4" merge-base --is-ancestor "$CLK_V2" "$HEAD4"
    check "promoted clk v2 is now in the tree" $?
fi

if bash "$CHECKER" --expected "$OUT/tested-set.txt" --actual "$OUT4/tested-set.txt" \
        > "$WORK/chk-d.log" 2>&1; then
    no "checker must reject a tree built from a different pin"
else
    ok "checker rejects a tree built from a different pin"
fi
grep -q 'tech/bsp/clk' "$WORK/chk-d.log"
check "checker names the topic whose sha moved" $?

# ==========================================================================
say ""
say "[6] error handling"

CFG_BAD="$WORK/unpinned.yaml"
sed "s/    sha: $DRM_V2/    sha: null/" "$CFG" > "$CFG_BAD"
KDIR5="$WORK/kernel-e"
fresh_kernel "$KDIR5"
if run_generate "$CFG_BAD" "$KDIR5" "$WORK/out-e" "$WORK/gen-e.log"; then
    no "generation must refuse a config with an unpinned topic"
else
    ok "generation refuses a config with an unpinned topic"
fi
grep -q 'unpinned topic' "$WORK/gen-e.log"
check "unpinned topic is named in the error" $?

CFG_MISS="$WORK/missing.yaml"
write_config "$CFG_MISS" "0000000000000000000000000000000000000000"
KDIR6="$WORK/kernel-f"
fresh_kernel "$KDIR6"
if run_generate "$CFG_MISS" "$KDIR6" "$WORK/out-f" "$WORK/gen-f.log" --skip-fetch; then
    no "generation must refuse a sha that does not exist"
else
    ok "generation refuses a sha that does not exist"
fi
grep -qi 'missing pin' "$WORK/gen-f.log"
check "unreachable pin is reported" $?

KDIR7="$WORK/kernel-g"
fresh_kernel "$KDIR7"
echo "dirty" > "$KDIR7/dirty-file.txt"
G "$KDIR7" add -A
if run_generate "$CFG" "$KDIR7" "$WORK/out-g" "$WORK/gen-g.log"; then
    no "generation must refuse a dirty worktree by default"
else
    ok "generation refuses a dirty worktree by default"
fi

if bash "$GENERATE" --config "$CFG" --kernel-dir "$WORK" --automerge "$AUTOMERGE_DIR" \
        --out-dir "$WORK/out-h" > "$WORK/gen-h.log" 2>&1; then
    no "generation must reject a non-kernel directory"
else
    ok "generation rejects a non-kernel directory"
fi

TAMPER="$WORK/tampered-tested-set.txt"
sed 's/^topic\ttech\/mm\/drm\t.*/topic\ttech\/mm\/drm\t1111111111111111111111111111111111111111/' \
    "$OUT/tested-set.txt" > "$TAMPER"
if bash "$CHECKER" --expected "$TAMPER" --actual "$OUT2/tested-set.txt" \
        > "$WORK/chk-tamper.log" 2>&1; then
    no "checker must reject a tampered tested-set"
else
    ok "checker rejects a tampered tested-set"
fi
grep -qi 'inconsistent' "$WORK/chk-tamper.log"
check "tampering is reported as an inconsistent record" $?

# ==========================================================================
say ""
say "[7] dry run"

KDIR8="$WORK/kernel-i"
OUT8="$WORK/out-i"
fresh_kernel "$KDIR8"
run_generate "$CFG" "$KDIR8" "$OUT8" "$WORK/gen-i.log" --dry-run
check "dry run succeeds" $? "$WORK/gen-i.log"
[ -s "$OUT8/merge.conf" ]
check "dry run writes merge.conf" $?
if G "$KDIR8" rev-parse --verify --quiet refs/heads/qcom-next >/dev/null 2>&1; then
    no "dry run must not create the integration branch"
else
    ok "dry run does not create the integration branch"
fi
[ "$(G "$KDIR8" rev-parse refs/heads/override/tech/bsp/clk 2>/dev/null)" = "$CLK_V1" ]
check "dry run still creates the override pin refs" $?

# ==========================================================================
say ""
say "[8] unresolved conflict"

# Two topics add the same file with different content, so the merge cannot be
# resolved and no rerere resolution exists. Generation must fail and name both
# the topic and the path. This also covers the two log formats ci-merge
# revisions use:
#   "Merge conflict : <name> : <sha>"      with log-parser support
#   "Merge failed, <name> <url> <branch>"  everywhere else
#
# mkdir is required: checking out the base removes the empty drivers/
# directories, and a redirect into a missing directory fails silently here
# because this script does not use set -e.
make_conflict_topic() {   # make_conflict_topic <branch> <content>
    G "$UPSTREAM" checkout -q -b "$1" "$BASE_OLD"
    mkdir -p "$UPSTREAM/drivers"
    printf 'line1\n%s\nline3\n' "$2" > "$UPSTREAM/drivers/shared.c"
    G "$UPSTREAM" add -A
    G "$UPSTREAM" commit -q -m "$1: add shared.c"
    G "$UPSTREAM" rev-parse HEAD
}

CA=$(make_conflict_topic conflict/alpha ALPHA)
CB=$(make_conflict_topic conflict/beta BETA)
G "$UPSTREAM" checkout -q main

# Guard the fixture itself: if the file never landed there is nothing to
# conflict over and the rest of this section would pass for the wrong reason.
G "$UPSTREAM" cat-file -e "$CA:drivers/shared.c" 2>/dev/null
check "fixture: conflict/alpha carries drivers/shared.c" $?
G "$UPSTREAM" cat-file -e "$CB:drivers/shared.c" 2>/dev/null
check "fixture: conflict/beta carries drivers/shared.c" $?

CFG_CONF="$WORK/qcom-next-conflict.yaml"
cat > "$CFG_CONF" <<EOF
schema_version: 1

base:
  repo: "file://$UPSTREAM"
  branch: main
  tag: v7.3-rc6
  sha: $BASE_OLD

promotion_state: open

rerere_cache:
  repo: "file://$RERERE"
  ref: master

topics:
  - name: conflict/alpha
    repo: "file://$UPSTREAM"
    branch: conflict/alpha
    sha: $CA
    tag: null
    depends_on: []
  - name: conflict/beta
    repo: "file://$UPSTREAM"
    branch: conflict/beta
    sha: $CB
    tag: null
    depends_on: []
EOF

KDIR9="$WORK/kernel-conflict"
OUT9="$WORK/out-conflict"
fresh_kernel "$KDIR9"
if run_generate "$CFG_CONF" "$KDIR9" "$OUT9" "$WORK/gen-conflict.log"; then
    no "generation must fail on an unresolved conflict"
else
    ok "generation fails on an unresolved conflict"
fi

[ -s "$OUT9/conflicts.txt" ]
check "conflicts.txt is written" $?

grep -q 'conflict/beta' "$OUT9/conflicts.txt" 2>/dev/null
check "the conflicting topic is named" $? "$OUT9/conflicts.txt"

grep -q 'drivers/shared.c' "$OUT9/conflicts.txt" 2>/dev/null
check "the conflicting path is recorded" $? "$OUT9/conflicts.txt"

grep -q 'drivers/shared.c' "$OUT9/summary.md" 2>/dev/null
check "the path reaches summary.md for the PR" $? "$OUT9/summary.md"

# The count must not be reported as zero just because this ci-merge revision
# uses the other log format.
grep -qE 'conflicts   : [1-9]' "$WORK/gen-conflict.log"
check "the conflict count is non-zero" $? "$WORK/gen-conflict.log"

# ==========================================================================
say ""
say "============================================================"
printf 'ran %d checks: %d passed, %d failed\n' "$((PASS + FAIL))" "$PASS" "$FAIL"
if [ "$FAIL" -eq 0 ]; then
    say "RESULT: PASS"
    exit 0
fi
say "RESULT: FAIL"
say "(re-run with KEEP_WORK=1 to inspect $WORK)"
exit 1
