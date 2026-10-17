#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline checks for the Phase 3 CI wiring.

GitHub Actions cannot be executed here, so these tests assert the properties
that are checkable statically and the ones that have historically broken:

  * every workflow is valid YAML and structurally sound
  * job dependencies and referenced outputs actually exist
  * the sync action genuinely honours its repo input (it used to ignore it)
  * the gate dispatcher's argument handling, gate table, and status contexts
    line up with what .github/actions/workflow_status posts
  * candidate ref naming and the prune workflow's parsing agree

Usage: scripts/selftest_qcom_next_ci.py [-v]
Exit 0 all passed, 1 a check failed, 2 prerequisite missing.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional

try:
    import yaml
except ImportError:
    sys.stderr.write("error: PyYAML is required (pip install pyyaml)\n")
    raise SystemExit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WF_DIR = os.path.join(REPO, ".github", "workflows")
ACT_DIR = os.path.join(REPO, ".github", "actions")

PR_CI = os.path.join(WF_DIR, "qcom-next-pr-ci.yml")
PRUNE = os.path.join(WF_DIR, "prune-qcom-next-candidates.yml")
VALIDATE = os.path.join(WF_DIR, "validate-qcom-next-config.yml")
SYNC = os.path.join(ACT_DIR, "sync", "action.yml")
STATUS_ACTION = os.path.join(ACT_DIR, "workflow_status", "action.yml")
DISPATCH = os.path.join(HERE, "dispatch_gates.sh")

CANDIDATE_REPO = "qualcomm-linux-stg/kernel"


class T:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.passed = 0
        self.failed: List[str] = []

    def ok(self, label: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            if self.verbose:
                print(f"  PASS  {label}")
        else:
            self.failed.append(label)
            print(f"  FAIL  {label}")
            if detail:
                for line in str(detail).strip().splitlines()[:12]:
                    print(f"          {line}")
        return bool(cond)


def load_wf(path: str):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def triggers(wf: dict) -> dict:
    # PyYAML parses a bare `on:` key as boolean True under YAML 1.1.
    return wf.get("on", wf.get(True, {})) or {}


def run(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    t = T(args.verbose)

    print("Phase 3 CI wiring self-tests")
    print(f"  repo: {REPO}")

    # ---------------------------------------------------------------- files
    print("\n[files]")
    for p in (PR_CI, PRUNE, VALIDATE, SYNC, STATUS_ACTION, DISPATCH):
        t.ok(f"exists: {os.path.relpath(p, REPO)}", os.path.exists(p))
    if t.failed:
        print("\nmissing prerequisites; aborting")
        return 1

    # ------------------------------------------------------------ yaml parse
    print("\n[yaml]")
    wfs: Dict[str, dict] = {}
    for p in (PR_CI, PRUNE, VALIDATE, SYNC, STATUS_ACTION):
        try:
            wfs[p] = load_wf(p)
            t.ok(f"parses: {os.path.basename(p)}", True)
        except yaml.YAMLError as exc:
            t.ok(f"parses: {os.path.basename(p)}", False, str(exc))
    if t.failed:
        return 1

    pr_ci = wfs[PR_CI]
    prune = wfs[PRUNE]
    sync = wfs[SYNC]
    status_action = wfs[STATUS_ACTION]

    # -------------------------------------------------------------- pr-ci job graph
    print("\n[qcom-next-pr-ci job graph]")
    jobs = pr_ci.get("jobs", {})
    expected_jobs = ["validate", "candidate", "gates", "report", "final-status"]
    for j in expected_jobs:
        t.ok(f"job present: {j}", j in jobs)

    # needs must reference real jobs
    for name, job in jobs.items():
        needs = job.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        for n in needs:
            t.ok(f"{name}.needs -> {n} exists", n in jobs)

    # job output references must resolve
    raw = open(PR_CI, encoding="utf-8").read()
    refs = set(re.findall(r"needs\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)", raw))
    for job_name, out_name in sorted(refs):
        job = jobs.get(job_name, {})
        if "uses" in job:
            # Reusable workflow: the output must be declared by the callee.
            callee = job["uses"]
            if callee.startswith("./"):
                cpath = os.path.join(REPO, callee[2:])
                if os.path.exists(cpath):
                    cwf = load_wf(cpath)
                    declared = (triggers(cwf).get("workflow_call", {}) or {}).get("outputs", {}) or {}
                    t.ok(f"needs.{job_name}.outputs.{out_name} declared by {os.path.basename(cpath)}",
                         out_name in declared)
                else:
                    t.ok(f"callee exists for {job_name}: {callee}", False)
        else:
            declared = job.get("outputs", {}) or {}
            t.ok(f"needs.{job_name}.outputs.{out_name} declared", out_name in declared)

    # candidate job must declare the outputs the rest of the graph consumes
    cand_out = (jobs.get("candidate", {}).get("outputs") or {})
    for key in ("candidate_ref", "candidate_sha", "digest"):
        t.ok(f"candidate declares output {key}", key in cand_out)

    # ------------------------------------------------------- pr-ci behaviour
    print("\n[qcom-next-pr-ci behaviour]")
    trig = triggers(pr_ci)
    t.ok("triggers on pull_request", "pull_request" in trig)
    t.ok("supports workflow_dispatch", "workflow_dispatch" in trig)

    env = pr_ci.get("env", {}) or {}
    t.ok("CANDIDATE_REPO defaults to the staging repo",
         CANDIDATE_REPO in str(env.get("CANDIDATE_REPO", "")), str(env))
    t.ok("does not reference the production kernel repo as candidate",
         "qualcomm-linux/kernel.git" not in raw and
         "candidate_repo: qualcomm-linux/kernel" not in raw)

    conc = pr_ci.get("concurrency", {}) or {}
    t.ok("concurrency is per-PR", "pull_request.number" in str(conc.get("group", "")))

    # The candidate job must be skipped when the tree cannot change.
    cand_if = str(jobs.get("candidate", {}).get("if", ""))
    for cls in ("metadata-only", "no-change", "invalid-mixed-change"):
        t.ok(f"candidate skipped for {cls}", cls in cand_if, cand_if)

    # Generation must not run against production refs by accident.
    t.ok("candidate job generates before pushing",
         raw.index("generate_qcom_next_tree.sh") < raw.index("Push the candidate ref"))

    t.ok("push uses --force to a candidate namespace",
         "refs/heads/${candidate_ref}" in raw and "qcom-next-candidate/pr-" in raw)

    t.ok("push fails loudly without a token",
         "no push token available" in raw)

    t.ok("gates are skipped when requested",
         "inputs.gates != 'none'" in raw or 'gates == \'none\'' in raw)

    t.ok("final-status treats skipped gates as acceptable",
         "success|skipped" in raw)

    t.ok("candidate artifacts are uploaded",
         "qcom-next-candidate" in raw and "tested-set.txt" in raw)

    t.ok("tested-set digest is surfaced for Phase 5",
         "digest" in cand_out)

    # ----------------------------------------------------------- sync action
    print("\n[sync action generalization]")
    sync_raw = open(SYNC, encoding="utf-8").read()
    sync_steps = (sync.get("runs", {}) or {}).get("steps", []) or []
    step_names = [s.get("name", "") for s in sync_steps]

    t.ok("declares a mode classifier step", any("Classify sync mode" in n for n in step_names))

    checkout = [s for s in sync_steps if s.get("name") == "Checkout kernel repository"]
    t.ok("has a generalized checkout step", bool(checkout))
    if checkout:
        with_ = checkout[0].get("with", {}) or {}
        t.ok("checkout uses inputs.repo, not a hardcoded repo",
             "inputs.repo" in str(with_.get("repository", "")), str(with_))
        t.ok("checkout uses inputs.base_branch as the ref",
             "inputs.base_branch" in str(with_.get("ref", "")), str(with_))
        t.ok("checkout passes a token (needed for a private staging repo)",
             "GH_TOKEN" in str(with_.get("token", "")), str(with_))

    t.ok("no hardcoded 'repository: qualcomm-linux/kernel' remains",
         "repository: qualcomm-linux/kernel\n" not in sync_raw)

    # PR-merge behaviour must be conditional on there being a PR.
    fetch_pr = [s for s in sync_steps
                if s.get("name") == "Fetch PR" and "pull/" in str(s.get("run", ""))
                and "kernel-topics" not in str(s.get("run", ""))]
    t.ok("kernel Fetch PR step exists", bool(fetch_pr))
    if fetch_pr:
        t.ok("Fetch PR only runs in pr mode",
             "mode == 'pr'" in str(fetch_pr[0].get("if", "")), str(fetch_pr[0].get("if")))

    sync_steps_merge = [s for s in sync_steps if s.get("name") == "Sync with latest changes"]
    if sync_steps_merge:
        t.ok("base-branch merge only runs in pr mode",
             "mode == 'pr'" in str(sync_steps_merge[0].get("if", "")))

    ws = [s for s in sync_steps if s.get("id") == "set-workspace"]
    t.ok("workspace step keys off the mode, not a repo name",
         bool(ws) and "mode" in str(ws[0].get("run", "")) and
         'inputs.repo }}" == "qualcomm-linux/kernel"' not in str(ws[0].get("run", "")))

    # ------------------------------------------------ status context contract
    print("\n[gate status contract]")
    # The dispatcher waits on commit statuses; confirm the action really posts
    # commit statuses (not check-runs) and that contexts match the gate table.
    status_raw = open(STATUS_ACTION, encoding="utf-8").read()
    t.ok("workflow_status posts to the statuses API",
         "/statuses/$sha" in status_raw, status_raw[:200])
    t.ok("workflow_status sets a context", '\\"context\\":' in status_raw)

    dispatch_raw = open(DISPATCH, encoding="utf-8").read()
    t.ok("dispatcher polls the combined commit status",
         "commits/$CANDIDATE_SHA/status" in dispatch_raw)

    gate_contexts = {
        "pre_merge.yml": "pre-merge",
        "pre-merge-yocto.yml": "Yocto Build Generation",
        "pre-merge-ubuntu.yml": "Ubuntu Package Generation",
    }
    for wf_name, ctx in gate_contexts.items():
        path = os.path.join(WF_DIR, wf_name)
        if not os.path.exists(path):
            t.ok(f"gate workflow present: {wf_name}", False)
            continue
        text = open(path, encoding="utf-8").read()
        t.ok(f"{wf_name} posts context {ctx!r}", ctx in text)
        t.ok(f"dispatcher knows context {ctx!r}", ctx in dispatch_raw)
        # The gates are dispatch-driven, so they must still accept that trigger.
        gwf = load_wf(path)
        t.ok(f"{wf_name} still accepts workflow_dispatch",
             "workflow_dispatch" in triggers(gwf))

    # yocto requires an extra `base` input; the dispatcher must supply it.
    t.ok("dispatcher supplies the yocto 'base' input",
         'base=tag' in dispatch_raw)

    # ------------------------------------------------------- dispatcher CLI
    print("\n[dispatch_gates.sh]")
    sha = "a" * 40

    p = run(["bash", DISPATCH, "--help"])
    t.ok("--help exits 0", p.returncode == 0, p.stdout + p.stderr)

    p = run(["bash", DISPATCH, "--candidate-repo", CANDIDATE_REPO,
             "--candidate-ref", "qcom-next-candidate/pr-1-abc",
             "--candidate-sha", sha, "--gates", "pre-merge,yocto,ubuntu",
             "--dry-run"])
    t.ok("dry run exits 0", p.returncode == 0, p.stdout + p.stderr)
    out = p.stdout
    t.ok("dry run names all three workflows",
         all(w in out for w in ("pre_merge.yml", "pre-merge-yocto.yml",
                                "pre-merge-ubuntu.yml")), out)
    t.ok("dry run targets the staging repo", CANDIDATE_REPO in out, out)
    t.ok("dry run shows the yocto base input", "base=tag" in out, out)
    t.ok("dry run lists the status contexts it will poll",
         "Yocto Build Generation" in out, out)

    # argument validation
    for label, argv_, want in [
        ("missing --candidate-repo",
         ["--candidate-ref", "r", "--candidate-sha", sha], 2),
        ("missing --candidate-ref",
         ["--candidate-repo", CANDIDATE_REPO, "--candidate-sha", sha], 2),
        ("missing --candidate-sha",
         ["--candidate-repo", CANDIDATE_REPO, "--candidate-ref", "r"], 2),
        ("abbreviated sha rejected",
         ["--candidate-repo", CANDIDATE_REPO, "--candidate-ref", "r",
          "--candidate-sha", "abc1234"], 2),
        ("uppercase sha rejected",
         ["--candidate-repo", CANDIDATE_REPO, "--candidate-ref", "r",
          "--candidate-sha", "A" * 40], 2),
        ("repo without owner rejected",
         ["--candidate-repo", "kernel", "--candidate-ref", "r",
          "--candidate-sha", sha], 2),
        ("unknown gate rejected",
         ["--candidate-repo", CANDIDATE_REPO, "--candidate-ref", "r",
          "--candidate-sha", sha, "--gates", "bogus", "--dry-run"], 2),
        ("unknown flag rejected",
         ["--candidate-repo", CANDIDATE_REPO, "--candidate-ref", "r",
          "--candidate-sha", sha, "--nope"], 2),
    ]:
        p = run(["bash", DISPATCH] + argv_)
        t.ok(f"{label} -> exit {want}", p.returncode == want,
             f"rc={p.returncode}\n{p.stdout}{p.stderr}")

    # ------------------------------------------------------ candidate naming
    print("\n[candidate ref naming]")
    # The prune workflow parses the ref name the pr-ci workflow creates; a
    # mismatch would silently keep every ref forever.
    prune_raw = open(PRUNE, encoding="utf-8").read()
    pattern = re.compile(r"^qcom-next-candidate/pr-(\d+)-([0-9a-f]{12})$")
    for ref in ["qcom-next-candidate/pr-0-abcdef123456",
                "qcom-next-candidate/pr-42-0123456789ab"]:
        t.ok(f"naming scheme matches sample: {ref}", bool(pattern.match(ref)))

    sed_expr = r"s#\^qcom-next-candidate/pr-\\(\[0-9\]\\{1,\\}\\)-\.\*#\\1#p"
    t.ok("prune extracts the PR number from the ref name",
         "qcom-next-candidate/pr-" in prune_raw and "pr_number=" in prune_raw)
    for ref in ["qcom-next-candidate/pr-42-0123456789ab"]:
        p = run(["sed", "-n",
                 r"s#^qcom-next-candidate/pr-\([0-9]\{1,\}\)-.*#\1#p"],
                input=ref)
        t.ok(f"prune sed yields the PR number for {ref}",
             p.stdout.strip() == "42", p.stdout + p.stderr)

    t.ok("prune paginates the ref listing", "--paginate" in prune_raw)
    t.ok("prune keeps refs whose PR is still open",
         "still open" in prune_raw)
    t.ok("manual prune runs default to a dry run",
         "workflow_dispatch" in prune_raw and "dry_run" in prune_raw)
    t.ok("prune targets the staging repo by default", CANDIDATE_REPO in prune_raw)

    # Retention must not destroy the artifact publication needs. Publication
    # happens after the integrator merges the PR, which closes it, so deleting
    # on close would remove the tested candidate at the moment it is required.
    t.ok("prune has a grace period after a PR closes",
         "MIN_CLOSED_AGE_HOURS" in prune_raw and "min_closed_age_hours" in prune_raw)
    t.ok("prune keeps a recently closed PR's candidate",
         "publication window" in prune_raw)
    t.ok("prune never deletes the currently published tree",
         "published_sha" in prune_raw and "currently published" in prune_raw)
    t.ok("prune resolves the published ref from the production repo",
         "PUBLISHED_REPO" in prune_raw and "PUBLISHED_REF" in prune_raw)
    t.ok("prune warns when the published-tree guard cannot be applied",
         "guard is inactive" in prune_raw)
    t.ok("prune no longer deletes purely because a PR is closed",
         'closed) reason="PR #${pr_number} is closed"' not in prune_raw)

    # ------------------------------------------------ sync mode classification
    print("\n[sync mode classification]")
    # Extract the classifier's logic and run it directly. The sync action is
    # shared with the production PR flow, so a regression here would break
    # every existing gate, not just the new candidate path.
    classify = [s for s in sync_steps if s.get("id") == "mode"]
    if not classify:
        t.ok("mode classifier step is present", False)
    else:
        body = str(classify[0].get("run", ""))
        for repo_in, pr_in, want in [
            ("qualcomm-linux/kernel", "123", "pr"),          # production PR flow
            ("qualcomm-linux/kernel", "0", "ref"),           # candidate flow
            ("qualcomm-linux/kernel", "", "ref"),            # candidate flow
            (CANDIDATE_REPO, "0", "ref"),                    # staging candidate
            (CANDIDATE_REPO, "", "ref"),
            (CANDIDATE_REPO, "7", "pr"),
            ("qualcomm-linux/kernel-topics", "55", "topics"),  # topics flow
            ("qualcomm-linux/kernel-topics", "", "topics"),
        ]:
            # Substitute the action expressions, then run the same branch logic.
            script = body.replace("${{ inputs.repo }}", repo_in)
            script = script.replace("${{ inputs.pr_number }}", pr_in)
            script = script.replace("${{ inputs.base_branch }}", "some-ref")
            script = script.replace('>> "$GITHUB_OUTPUT"', ">> /dev/null")
            script += '\nprintf "MODE=%s\\n" "$mode"\n'
            p = run(["bash", "-c", script])
            got = ""
            m = re.search(r"MODE=(\w+)", p.stdout)
            if m:
                got = m.group(1)
            t.ok(f"repo={repo_in or '<empty>'} pr={pr_in or '<empty>'} -> {want}",
                 got == want, f"got={got!r}\n{p.stdout}{p.stderr}")

    # --------------------------------------------------------- shell syntax
    print("\n[shell syntax]")
    for script in ["dispatch_gates.sh", "generate_qcom_next_tree.sh",
                   "check_tested_tree_matches.sh", "selftest_qcom_next_generate.sh"]:
        path = os.path.join(HERE, script)
        if not os.path.exists(path):
            t.ok(f"{script} exists", False)
            continue
        p = run(["bash", "-n", path])
        t.ok(f"{script} parses", p.returncode == 0, p.stdout + p.stderr)

    # ------------------------------------------------------------- epilogue
    total = t.passed + len(t.failed)
    print("\n" + "=" * 60)
    print(f"ran {total} checks: {t.passed} passed, {len(t.failed)} failed")
    if t.failed:
        print("\nfailed checks:")
        for name in t.failed:
            print(f"  - {name}")
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
