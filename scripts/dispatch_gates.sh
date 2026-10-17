#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# Dispatch the kernel gate workflows against a generated candidate ref and wait
# for their verdicts.
#
# Why dispatch rather than `uses:`
# --------------------------------
# pre_merge.yml, pre-merge-yocto.yml and pre-merge-ubuntu.yml are
# workflow_dispatch-only, which is how they are already driven today. Two of
# them also read some inputs through `github.event.inputs.*`, which is populated
# for workflow_dispatch but empty under workflow_call, so converting them to
# reusable workflows would silently change their behaviour. Dispatching them
# uses the interface they already expose and leaves them untouched.
#
# How completion is detected
# --------------------------
# Each gate reports through .github/actions/workflow_status, which POSTs a
# *commit status* to /repos/<repo>/statuses/<sha> with `context` set to the
# gate's check name. Polling the combined status of the candidate commit is
# therefore an exact correlation: it needs no run-id guessing and no matching on
# run titles, and it keeps working if a gate is retried.
#
# Usage:
#   dispatch_gates.sh --candidate-repo qualcomm-linux-stg/kernel \
#                     --candidate-ref qcom-next-candidate/pr-42-abc123 \
#                     --candidate-sha <sha> \
#                     --gates pre-merge,yocto,ubuntu
#
# Exit status:
#   0  every requested gate reported success
#   1  at least one gate failed, or the wait timed out
#   2  usage error or missing prerequisite
set -u
set -o pipefail

CANDIDATE_REPO=""
CANDIDATE_REF=""
CANDIDATE_SHA=""
WORKFLOW_REPO="qualcomm-linux/kernel-config"
GATES="pre-merge"
PR_NUMBER="0"
TIMEOUT=10800          # 3h; a full kernel build plus LAVA can be slow
POLL_INTERVAL=60
SETTLE=120             # grace period for the first pending status to appear
DRY_RUN=0
OUTPUT=""
VERBOSE=0

die()  { printf 'error: %s\n' "$*" >&2; exit 2; }
fail() { printf 'FAILED: %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
dbg()  { [ "$VERBOSE" -eq 1 ] && printf '    %s\n' "$*" || true; }

usage() {
    cat <<'EOF'
Dispatch the kernel gates against a candidate ref and wait for their verdicts.

Required:
  --candidate-repo OWNER/REPO   repo holding the candidate ref
  --candidate-ref REF           branch pushed by candidate generation
  --candidate-sha SHA           commit the gates must report against

Options:
  --workflow-repo OWNER/REPO    repo owning the gate workflows
                                (default: qualcomm-linux/kernel-config)
  --gates LIST                  comma-separated: pre-merge, yocto, ubuntu
                                (default: pre-merge)
  --pr NUMBER                   PR number passed to the gates (default: 0)
  --timeout SECONDS             overall wait budget (default: 10800)
  --poll-interval SECONDS       status poll interval (default: 60)
  --settle SECONDS              grace period for the first status (default: 120)
  --output PATH                 write a Markdown summary here
  --dry-run                     print what would be dispatched and exit
  -v, --verbose
  -h, --help

Requires the gh CLI, authenticated with a token that can dispatch workflows in
--workflow-repo and read commit statuses in --candidate-repo.

Gate -> workflow file -> status context:
  pre-merge  pre_merge.yml         "pre-merge"
  yocto      pre-merge-yocto.yml   "Yocto Build Generation"
  ubuntu     pre-merge-ubuntu.yml  "Ubuntu Package Generation"

Exit 0 all gates green, 1 a gate failed or timed out, 2 usage error.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --candidate-repo) CANDIDATE_REPO="${2:?}"; shift 2 ;;
        --candidate-ref)  CANDIDATE_REF="${2:?}"; shift 2 ;;
        --candidate-sha)  CANDIDATE_SHA="${2:?}"; shift 2 ;;
        --workflow-repo)  WORKFLOW_REPO="${2:?}"; shift 2 ;;
        --gates)          GATES="${2:?}"; shift 2 ;;
        --pr)             PR_NUMBER="${2:?}"; shift 2 ;;
        --timeout)        TIMEOUT="${2:?}"; shift 2 ;;
        --poll-interval)  POLL_INTERVAL="${2:?}"; shift 2 ;;
        --settle)         SETTLE="${2:?}"; shift 2 ;;
        --output)         OUTPUT="${2:?}"; shift 2 ;;
        --dry-run)        DRY_RUN=1; shift ;;
        -v|--verbose)     VERBOSE=1; shift ;;
        -h|--help)        usage; exit 0 ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

[ -n "$CANDIDATE_REPO" ] || die "--candidate-repo is required"
[ -n "$CANDIDATE_REF" ]  || die "--candidate-ref is required"
[ -n "$CANDIDATE_SHA" ]  || die "--candidate-sha is required"

case "$CANDIDATE_REPO" in */*) ;; *) die "--candidate-repo must be OWNER/REPO: $CANDIDATE_REPO" ;; esac
case "$WORKFLOW_REPO"  in */*) ;; *) die "--workflow-repo must be OWNER/REPO: $WORKFLOW_REPO" ;; esac
case "$CANDIDATE_SHA" in
    [0-9a-f]*) [ "${#CANDIDATE_SHA}" -eq 40 ] || die "--candidate-sha must be a full 40-hex sha" ;;
    *) die "--candidate-sha must be lowercase 40-hex: $CANDIDATE_SHA" ;;
esac

# gate -> "workflow file|status context"
gate_spec() {
    case "$1" in
        pre-merge) echo "pre_merge.yml|pre-merge" ;;
        yocto)     echo "pre-merge-yocto.yml|Yocto Build Generation" ;;
        ubuntu)    echo "pre-merge-ubuntu.yml|Ubuntu Package Generation" ;;
        *)         echo "" ;;
    esac
}

SELECTED=()
IFS=',' read -r -a REQUESTED <<< "$GATES"
for g in "${REQUESTED[@]}"; do
    g="$(printf '%s' "$g" | tr -d '[:space:]')"
    [ -n "$g" ] || continue
    spec="$(gate_spec "$g")"
    [ -n "$spec" ] || die "unknown gate '$g' (valid: pre-merge, yocto, ubuntu)"
    SELECTED+=("$g|$spec")
done
[ "${#SELECTED[@]}" -gt 0 ] || die "no gates selected"

step "Plan"
info "candidate repo : $CANDIDATE_REPO"
info "candidate ref  : $CANDIDATE_REF"
info "candidate sha  : $CANDIDATE_SHA"
info "workflow repo  : $WORKFLOW_REPO"
info "gates          : ${#SELECTED[@]}"
for entry in "${SELECTED[@]}"; do
    IFS='|' read -r name wf ctx <<< "$entry"
    info "  $name -> $wf (status context: \"$ctx\")"
done

if [ "$DRY_RUN" -eq 1 ]; then
    step "Dry run: commands that would be issued"
    for entry in "${SELECTED[@]}"; do
        IFS='|' read -r name wf ctx <<< "$entry"
        info "gh workflow run $wf --repo $WORKFLOW_REPO \\"
        info "    -f pr=$PR_NUMBER \\"
        info "    -f sha=$CANDIDATE_SHA \\"
        info "    -f ref=$CANDIDATE_REF \\"
        if [ "$name" = "yocto" ]; then
            info "    -f repo=$CANDIDATE_REPO \\"
            info "    -f base=tag"
        else
            info "    -f repo=$CANDIDATE_REPO"
        fi
    done
    info ""
    info "then poll: gh api repos/$CANDIDATE_REPO/commits/$CANDIDATE_SHA/status"
    info "until these contexts report success or failure:"
    for entry in "${SELECTED[@]}"; do
        IFS='|' read -r name wf ctx <<< "$entry"
        info "    \"$ctx\""
    done
    exit 0
fi

command -v gh >/dev/null 2>&1 || die "gh CLI not found in PATH"
command -v jq >/dev/null 2>&1 || die "jq not found in PATH"

step "Verifying the candidate commit is visible"
if ! gh api "repos/$CANDIDATE_REPO/commits/$CANDIDATE_SHA" --jq '.sha' >/dev/null 2>&1; then
    fail "commit $CANDIDATE_SHA is not present in $CANDIDATE_REPO. The candidate ref must be pushed before the gates are dispatched."
fi
info "commit is visible in $CANDIDATE_REPO"

# --------------------------------------------------------------------------
step "Dispatching gates"

DISPATCH_FAILED=0
for entry in "${SELECTED[@]}"; do
    IFS='|' read -r name wf ctx <<< "$entry"
    info "dispatching $name ($wf)"
    args=(workflow run "$wf" --repo "$WORKFLOW_REPO"
          -f "pr=$PR_NUMBER"
          -f "sha=$CANDIDATE_SHA"
          -f "ref=$CANDIDATE_REF"
          -f "repo=$CANDIDATE_REPO")
    # pre-merge-yocto.yml additionally requires `base`.
    if [ "$name" = "yocto" ]; then
        args+=(-f "base=tag")
    fi
    if ! gh "${args[@]}" 2>&1 | sed 's/^/    /'; then
        printf 'failed to dispatch %s (%s)\n' "$name" "$wf" >&2
        DISPATCH_FAILED=$((DISPATCH_FAILED + 1))
    fi
done

[ "$DISPATCH_FAILED" -eq 0 ] || fail "$DISPATCH_FAILED gate(s) could not be dispatched"

# --------------------------------------------------------------------------
step "Waiting for gate verdicts"
info "polling the commit status of $CANDIDATE_SHA every ${POLL_INTERVAL}s (budget ${TIMEOUT}s)"

status_json() {
    gh api "repos/$CANDIDATE_REPO/commits/$CANDIDATE_SHA/status" 2>/dev/null || echo '{}'
}

context_state() {   # context_state <json> <context>
    printf '%s' "$1" | jq -r --arg c "$2" \
        '[.statuses[]? | select(.context == $c)] | sort_by(.updated_at) | last | .state // "missing"'
}

context_url() {
    printf '%s' "$1" | jq -r --arg c "$2" \
        '[.statuses[]? | select(.context == $c)] | sort_by(.updated_at) | last | .target_url // ""'
}

ELAPSED=0
declare -A FINAL_STATE
declare -A FINAL_URL
for entry in "${SELECTED[@]}"; do
    IFS='|' read -r name wf ctx <<< "$entry"
    FINAL_STATE["$name"]=""
    FINAL_URL["$name"]=""
done

while :; do
    json="$(status_json)"
    pending=0
    for entry in "${SELECTED[@]}"; do
        IFS='|' read -r name wf ctx <<< "$entry"
        [ -n "${FINAL_STATE[$name]}" ] && continue

        state="$(context_state "$json" "$ctx")"
        dbg "$name ($ctx): $state"

        case "$state" in
            success|failure|error)
                FINAL_STATE["$name"]="$state"
                FINAL_URL["$name"]="$(context_url "$json" "$ctx")"
                info "  $name -> $state"
                ;;
            pending)
                pending=$((pending + 1))
                ;;
            missing)
                # The gate has not posted its initial pending status yet. That
                # is normal right after dispatch while the runner starts up,
                # but past the settle window it means the gate never started.
                if [ "$ELAPSED" -ge "$SETTLE" ]; then
                    FINAL_STATE["$name"]="never-started"
                    info "  $name -> no status posted within ${SETTLE}s"
                else
                    pending=$((pending + 1))
                fi
                ;;
            *)
                pending=$((pending + 1))
                ;;
        esac
    done

    [ "$pending" -eq 0 ] && break

    if [ "$ELAPSED" -ge "$TIMEOUT" ]; then
        for entry in "${SELECTED[@]}"; do
            IFS='|' read -r name wf ctx <<< "$entry"
            [ -n "${FINAL_STATE[$name]}" ] || FINAL_STATE["$name"]="timeout"
        done
        break
    fi

    sleep "$POLL_INTERVAL"
    ELAPSED=$((ELAPSED + POLL_INTERVAL))
    dbg "elapsed ${ELAPSED}s, $pending gate(s) still pending"
done

# --------------------------------------------------------------------------
step "Results"

GREEN=0
RED=0
for entry in "${SELECTED[@]}"; do
    IFS='|' read -r name wf ctx <<< "$entry"
    state="${FINAL_STATE[$name]:-unknown}"
    if [ "$state" = "success" ]; then
        GREEN=$((GREEN + 1))
    else
        RED=$((RED + 1))
    fi
    printf '  %-10s %-14s %s\n' "$name" "$state" "${FINAL_URL[$name]}"
done

if [ -n "$OUTPUT" ]; then
    {
        echo "### Candidate gate results"
        echo ""
        echo "| Gate | Result | Run |"
        echo "|---|---|---|"
        for entry in "${SELECTED[@]}"; do
            IFS='|' read -r name wf ctx <<< "$entry"
            state="${FINAL_STATE[$name]:-unknown}"
            url="${FINAL_URL[$name]}"
            if [ -n "$url" ]; then
                echo "| \`$name\` | \`$state\` | [run]($url) |"
            else
                echo "| \`$name\` | \`$state\` | n/a |"
            fi
        done
        echo ""
        echo "Candidate: \`$CANDIDATE_REPO\` @ \`$CANDIDATE_REF\` (\`${CANDIDATE_SHA:0:12}\`)"
    } > "$OUTPUT"
    info "summary written to $OUTPUT"
fi

if [ "$RED" -gt 0 ]; then
    fail "$RED of $((GREEN + RED)) gate(s) did not succeed"
fi
info "RESULT: PASS - all $GREEN gate(s) succeeded"
exit 0
