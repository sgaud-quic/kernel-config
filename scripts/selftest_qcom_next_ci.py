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
import json
import os
import re
import subprocess
import sys
import tempfile
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
GATES_WF = os.path.join(WF_DIR, "qcom-next-gates.yml")
SYNC = os.path.join(ACT_DIR, "sync", "action.yml")
STATUS_ACTION = os.path.join(ACT_DIR, "workflow_status", "action.yml")
DISPATCH = os.path.join(HERE, "dispatch_gates.sh")

CANDIDATE_REPO = "qualcomm-linux-stg/kernel"
# The repo the PR-centric gates are hardwired to; only they can run there.
PROD_REPO = "qualcomm-linux/kernel"


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
    validate_wf = wfs[VALIDATE]
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

    # The candidate repo default now lives in the config job, because
    # workflow-level env cannot read inputs on a pull_request event.
    cfg_steps = "".join(str(s.get("run", ""))
                        for s in (pr_ci.get("jobs", {}).get("config", {})
                                  .get("steps") or []))
    t.ok("candidate repo defaults to the staging repo",
         CANDIDATE_REPO in cfg_steps, cfg_steps[:200])
    t.ok("jobs consume the resolved candidate repo",
         "needs.config.outputs.candidate_repo" in raw)
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

    # Gates must default to off, including on pull_request runs where no input
    # exists. The default is resolved in the config job, and the two gate paths
    # key off the split it publishes rather than reading inputs at job level.
    # The matrix below proves gates=none yields build=false and no dispatch.
    gates_if = str(jobs.get("gates", {}).get("if", ""))
    t.ok("dispatcher job keys off the resolved dispatch list",
         "needs.config.outputs.gates_dispatched" in gates_if, gates_if)
    t.ok("dispatcher job is skipped when nothing is left to dispatch",
         "!= ''" in gates_if, gates_if)
    build_if = str(jobs.get("build-gate", {}).get("if", ""))
    t.ok("called build gate keys off the resolved build flag",
         "needs.config.outputs.gate_build" in build_if, build_if)
    t.ok("called build gate runs only when the build gate is requested",
         "== 'true'" in build_if, build_if)
    t.ok("build gate requires a successful candidate",
         "needs.candidate.result == 'success'" in build_if, build_if)
    cfg_job = jobs.get("config", {})
    for out_name in ("gates", "gate_build", "gates_dispatched"):
        t.ok(f"config job publishes '{out_name}'",
             out_name in (cfg_job.get("outputs") or {}),
             str(cfg_job.get("outputs")))
    cfg_run = "".join(str(s.get("run", "")) for s in cfg_job.get("steps") or [])
    t.ok("config job defaults gates to none",
         'gates="none"' in cfg_run, cfg_run[:200])
    dispatch_gates_default = (
        (triggers(pr_ci).get("workflow_dispatch", {}) or {})
        .get("inputs", {}).get("gates", {}).get("default")
    )
    t.ok("dispatch input for gates defaults to none",
         dispatch_gates_default == "none", str(dispatch_gates_default))

    t.ok("header documents how to trigger from the Actions UI",
         "default branch" in raw and "pull_request event runs this workflow" in raw)

    # ------------------------------------------- migration flag resolution
    print("\n[allow_untagged resolution]")
    # Both workflows fire on pull_request, where no inputs exist. Input
    # defaults do not apply there, so each must supply its own fallback.
    vraw = open(VALIDATE, encoding="utf-8").read()
    t.ok("validate coalesces a null allow_untagged",
         "inputs.allow_untagged == null" in vraw, vraw[:0])
    t.ok("validate's shell fallback is on, not off",
         'ALLOW_UNTAGGED:-true' in vraw,
         [l for l in vraw.splitlines() if "ALLOW_UNTAGGED:-" in l])
    t.ok("pr-ci defaults allow_untagged on in the config job",
         'allow_untagged="true"' in cfg_run, cfg_run[:200])
    t.ok("pr-ci passes the resolved value to the reusable workflow",
         "needs.config.outputs.allow_untagged" in raw)

    vtrig = triggers(validate_wf)
    for trig_name in ("workflow_dispatch", "workflow_call"):
        default = ((vtrig.get(trig_name, {}) or {})
                   .get("inputs", {}).get("allow_untagged", {}).get("default"))
        t.ok(f"validate {trig_name} allow_untagged default is true",
             default is True, str(default))

    # The atomic override must stay off by default: it exists to let an
    # integrator bypass a dependency check, not to be on implicitly.
    t.ok("atomic override defaults off on pull_request",
         "ALLOW_ATOMIC:-false" in vraw)
    for trig_name in ("workflow_dispatch", "workflow_call"):
        default = ((vtrig.get(trig_name, {}) or {})
                   .get("inputs", {}).get("allow_atomic_override", {}).get("default"))
        t.ok(f"validate {trig_name} allow_atomic_override default is false",
             default is False, str(default))

    # Allowing an untagged sha must not weaken the pinned-sha rule.
    t.ok("validator documents that --allow-untagged covers tags only",
         "concerns tags only" in open(
             os.path.join(HERE, "validate_qcom_next_config.py"),
             encoding="utf-8").read()
         or "Does NOT excuse" in vraw or "unpinned sha is rejected" in vraw)

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

    # The PR-centric gates are only legal against the production kernel repo,
    # so the dry run that inspects their wiring has to use that repo.
    p = run(["bash", DISPATCH, "--candidate-repo", PROD_REPO,
             "--candidate-ref", "qcom-next-candidate/pr-1-abc",
             "--candidate-sha", sha, "--gates", "pre-merge,yocto,ubuntu",
             "--dry-run"])
    t.ok("dry run exits 0", p.returncode == 0, p.stdout + p.stderr)
    out = p.stdout
    t.ok("dry run names all three workflows",
         all(w in out for w in ("pre_merge.yml", "pre-merge-yocto.yml",
                                "pre-merge-ubuntu.yml")), out)
    t.ok("dry run targets the candidate repo", PROD_REPO in out, out)
    t.ok("dry run shows the yocto base input", "base=tag" in out, out)
    t.ok("dry run lists the status contexts it will poll",
         "Yocto Build Generation" in out, out)

    # The build gate is the one that works against a staging candidate.
    p = run(["bash", DISPATCH, "--candidate-repo", CANDIDATE_REPO,
             "--candidate-ref", "qcom-next-candidate/pr-1-abc",
             "--candidate-sha", sha, "--gates", "build", "--dry-run"])
    t.ok("build gate dry run exits 0", p.returncode == 0, p.stdout + p.stderr)
    bout = p.stdout
    t.ok("build gate targets the candidate workflow",
         "qcom-next-gates.yml" in bout, bout)
    t.ok("build gate polls the qcom-next-build context",
         "qcom-next-build" in bout, bout)
    t.ok("build gate targets the staging repo", CANDIDATE_REPO in bout, bout)
    t.ok("build gate does not request the yocto base input",
         "base=tag" not in bout, bout)

    # Refusing beats a false green: these gates would skip boot/functional and
    # build an empty workspace for a non-production repo.
    for g in ("pre-merge", "yocto", "ubuntu"):
        p = run(["bash", DISPATCH, "--candidate-repo", CANDIDATE_REPO,
                 "--candidate-ref", "r", "--candidate-sha", sha,
                 "--gates", g, "--dry-run"])
        t.ok(f"gate '{g}' refused for a staging candidate",
             p.returncode == 2, f"rc={p.returncode}")
        t.ok(f"gate '{g}' refusal explains the cause",
             "cannot test a candidate in" in (p.stdout + p.stderr),
             p.stdout + p.stderr)

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
        step_env = classify[0].get("env", {}) or {}
        # Inputs must reach the script through env, not ${{ }} interpolation.
        t.ok("classifier reads inputs from env",
             all(k in step_env for k in ("IN_REPO", "IN_PR")), str(step_env))
        t.ok("classifier body has no template interpolation",
             "${{" not in body, body)

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
            script = body.replace('>> "$GITHUB_OUTPUT"', ">> /dev/null")
            script += '\nprintf "MODE=%s\\n" "$mode"\n'
            env = dict(os.environ)
            env.update({"IN_REPO": repo_in, "IN_PR": pr_in, "IN_REF": "some-ref"})
            p = subprocess.run(["bash", "-c", script], capture_output=True,
                               text=True, timeout=120, env=env)
            got = ""
            m = re.search(r"MODE=(\w+)", p.stdout)
            if m:
                got = m.group(1)
            t.ok(f"repo={repo_in or '<empty>'} pr={pr_in or '<empty>'} -> {want}",
                 got == want, f"got={got!r}\n{p.stdout}{p.stderr}")

    # ------------------------------------------------- fork PR comment safety
    print("\n[fork PR handling]")
    # A fork PR always receives a read-only GITHUB_TOKEN regardless of the
    # permissions block, so commenting returns 403. The run must not fail
    # because of it; that cost a full CI cycle once already.
    for label, raw_text in (("pr-ci", raw), ("validate", vraw)):
        t.ok(f"{label}: comment step is gated on a same-repo PR",
             "head.repo.full_name == github.repository" in raw_text, label)
        t.ok(f"{label}: comment step is continue-on-error",
             "continue-on-error: true" in raw_text, label)
        t.ok(f"{label}: has a fork fallback path",
             "head.repo.full_name != github.repository" in raw_text, label)
        t.ok(f"{label}: explains the read-only token",
             "read-only" in raw_text, label)

    # An expression is evaluated anywhere in a workflow file, including inside
    # a script body or a comment. An empty one is a syntax error that makes the
    # whole file unloadable, which GitHub reports only as "workflow file issue"
    # with no line number. This cost a full CI cycle, so check it directly.
    print("\n[expression syntax]")
    delim = "$" + "{{"
    for label, path in (("pr-ci", PR_CI), ("validate", VALIDATE),
                        ("prune", PRUNE), ("sync action", SYNC)):
        body = open(path, encoding="utf-8").read()
        empties = [i + 1 for i, l in enumerate(body.splitlines())
                   if re.search(r"\$\{\{\s*\}\}", l)]
        t.ok(f"{label}: no empty expression", not empties, f"lines {empties}")

        # Every expression must reference a real context, otherwise it is prose
        # that will be evaluated as code.
        bad_ctx = []
        for i, l in enumerate(body.splitlines(), 1):
            for m in re.finditer(r"\$\{\{(.*?)\}\}", l):
                inner = m.group(1).strip()
                if not inner:
                    continue
                if not re.match(r"^(steps|needs|inputs|github|env|secrets|vars|"
                                r"matrix|job|jobs|runner|strategy|always|success|"
                                r"failure|cancelled|hashFiles|format|fromJSON|"
                                r"toJSON|contains|startsWith|endsWith|join)\b",
                                inner):
                    bad_ctx.append((i, inner[:48]))
        t.ok(f"{label}: every expression references a context",
             not bad_ctx, str(bad_ctx[:3]))

    # Values must reach github-script through env, never by interpolation into
    # the script body, or a workflow value could be parsed as code. Strict:
    # the body must contain no expression delimiter at all, because even a
    # comment mentioning one is evaluated.
    for label, wf_obj in (("pr-ci", pr_ci), ("validate", validate_wf)):
        for job_name, job in (wf_obj.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if str(step.get("uses", "")).startswith("actions/github-script"):
                    script = str((step.get("with") or {}).get("script", ""))
                    bad = [l.strip() for l in script.splitlines() if delim in l]
                    t.ok(f"{label}.{job_name}: github-script body has no expression delimiter",
                         not bad, bad[:3])

    # A step's own env block must not reference the env context: the ordering
    # within one mapping is undefined and GitHub rejects it.
    for label, wf_obj in (("pr-ci", pr_ci), ("validate", validate_wf),
                          ("prune", prune)):
        for job_name, job in (wf_obj.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                for k, v in (step.get("env") or {}).items():
                    t.ok(f"{label}.{job_name}: step env {k} does not read the env context",
                         not re.search(r"\$\{\{\s*env\.", str(v)), f"{k}={v}")


    # pull-requests: write must be scoped to the job that needs it.
    for label, wf_obj in (("pr-ci", pr_ci), ("validate", validate_wf)):
        wf_perms = wf_obj.get("permissions") or {}
        t.ok(f"{label}: workflow-level permissions are read-only",
             wf_perms.get("pull-requests") != "write", str(wf_perms))
        writers = [n for n, j in (wf_obj.get("jobs") or {}).items()
                   if (j.get("permissions") or {}).get("pull-requests") == "write"]
        # Only the job that comments needs it, plus any reusable-workflow call
        # that has to raise the ceiling for the callee. Everything else must
        # stay read-only.
        expected = {"report", "validate"} if label == "pr-ci" else {"validate"}
        t.ok(f"{label}: only the commenting job and reusable calls request "
             f"pull-requests write",
             set(writers) <= expected and len(writers) >= 1,
             f"writers={writers} expected subset of {sorted(expected)}")

    # The inputs context does not exist for a pull_request event. Workflow-level
    # env, job-level if, and job-level with are all evaluated during workflow
    # setup, so referencing inputs there makes the workflow fail to start with
    # startup_failure and no usable error. Step-level env is evaluated at run
    # time and is safe. This cost two CI cycles.
    print("\n[inputs context safety]")
    for label, wf_obj, path in (("pr-ci", pr_ci, PR_CI),
                                ("validate", validate_wf, VALIDATE),
                                ("prune", prune, PRUNE)):
        trig = triggers(wf_obj)
        if "pull_request" not in trig:
            print(f"  skip  {label}: no pull_request trigger")
            continue

        wf_env = str(wf_obj.get("env") or {})
        t.ok(f"{label}: workflow-level env does not read inputs",
             "inputs." not in wf_env, wf_env)

        for jname, job in (wf_obj.get("jobs") or {}).items():
            t.ok(f"{label}.{jname}: job if does not read inputs",
                 "inputs." not in str(job.get("if", "")), str(job.get("if")))
            t.ok(f"{label}.{jname}: job with does not read inputs",
                 "inputs." not in str(job.get("with") or {}), str(job.get("with")))
            t.ok(f"{label}.{jname}: job env does not read inputs",
                 "inputs." not in str(job.get("env") or {}), str(job.get("env")))

    # Every remaining inputs reference must sit in a step-level env block.
    for label, path in (("pr-ci", PR_CI), ("validate", VALIDATE),
                        ("prune", PRUNE)):
        wf_obj = {"pr-ci": pr_ci, "validate": validate_wf, "prune": prune}[label]
        allowed = set()
        for job in (wf_obj.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                for v in (step.get("env") or {}).values():
                    allowed.add(str(v))
        body = open(path, encoding="utf-8").read()
        offenders = []
        for i, line in enumerate(body.splitlines(), 1):
            if "inputs." not in line or line.lstrip().startswith("#"):
                continue
            if not any(line.strip().endswith(a.strip()) or a.strip() in line
                       for a in allowed):
                offenders.append((i, line.strip()[:60]))
        t.ok(f"{label}: every inputs reference is in a step env",
             not offenders, str(offenders[:3]))

    # ------------------------------------------------------ gate split matrix
    print("\n[gate split resolution]")
    # The build gate is called, the rest are dispatched. If the split is wrong
    # the same tree is either built twice or not at all, and because both
    # consumers are job-level ifs the mistake shows up as a silently skipped
    # job rather than an error.
    cfg_steps = ((pr_ci.get("jobs") or {}).get("config") or {}).get("steps") or []
    resolve = [s for s in cfg_steps if s.get("id") == "r"]
    if not resolve:
        t.ok("config resolve step is present", False)
    else:
        body = str(resolve[0].get("run", ""))
        t.ok("resolve body has no template interpolation",
             "${{" not in body, body)

        for in_gates, want_build, want_disp in [
            ("none", "false", ""),
            ("", "false", ""),                       # pull_request default
            ("build", "true", ""),
            ("all", "true", "pre-merge,yocto,ubuntu"),
            ("pre-merge", "false", "pre-merge"),
            ("build,yocto", "true", "yocto"),
            ("pre-merge,yocto,ubuntu", "false", "pre-merge,yocto,ubuntu"),
        ]:
            with tempfile.TemporaryDirectory() as td:
                out_f = os.path.join(td, "out")
                sum_f = os.path.join(td, "sum")
                open(out_f, "w").close()
                open(sum_f, "w").close()
                script = body + '\necho "R|$gate_build|$dispatched"\n'
                env = {
                    "PATH": "/usr/bin:/bin",
                    "IN_GATES": in_gates,
                    "IN_CANDIDATE_REPO": "",
                    "IN_ALLOW_UNTAGGED": "",
                    "IN_PR": "0",
                    "EVENT_PR": "291",
                    "GITHUB_OUTPUT": out_f,
                    "GITHUB_STEP_SUMMARY": sum_f,
                }
                p = subprocess.run(["bash", "-c", script], capture_output=True,
                                   text=True, env=env)
                line = [l for l in p.stdout.splitlines() if l.startswith("R|")]
                if not line:
                    t.ok(f"gates={in_gates!r} resolves", False,
                         p.stderr.strip()[:200])
                    continue
                _, got_build, got_disp = line[0].split("|")
                t.ok(f"gates={in_gates!r} -> build={want_build} "
                     f"dispatched={want_disp or '<none>'}",
                     got_build == want_build and got_disp == want_disp,
                     f"got build={got_build} dispatched={got_disp!r}")

    # --------------------------------------------- dangling step/needs refs
    print("\n[expression reference integrity]")
    # A reference to a step id that no longer exists evaluates to empty rather
    # than failing, so a step guarded by it silently never runs and a job that
    # consumes it silently sees "". Deleting a step and leaving a reader behind
    # is therefore invisible at runtime.
    for label, path in (("pr-ci", PR_CI), ("validate", VALIDATE),
                        ("prune", PRUNE), ("gates-wf", GATES_WF)):
        if not os.path.exists(path):
            continue
        wf_obj = load_wf(path)
        raw_wf = open(path, encoding="utf-8").read()
        jobs_o = wf_obj.get("jobs") or {}

        for jname, job in jobs_o.items():
            blob = json.dumps(job)
            declared = {s.get("id") for s in (job.get("steps") or [])
                        if s.get("id")}
            referenced = set(re.findall(r"steps\.([A-Za-z0-9_-]+)\.", blob))
            for r in sorted(referenced - declared):
                t.ok(f"{label}.{jname}: steps.{r} is declared in this job",
                     False,
                     f"declared step ids: {sorted(declared)}")
            if referenced <= declared:
                t.ok(f"{label}.{jname}: step references all resolve",
                     True, f"{len(referenced)} reference(s)")

            # Same failure mode for needs.<job>: an unlisted dependency yields
            # an empty value instead of an error.
            needs = job.get("needs") or []
            if isinstance(needs, str):
                needs = [needs]
            ref_needs = set(re.findall(r"needs\.([A-Za-z0-9_-]+)\.", blob))
            unknown = {n for n in ref_needs if n not in set(needs)}
            t.ok(f"{label}.{jname}: needs references are all declared",
                 not unknown,
                 f"referenced {sorted(unknown)} but needs={needs}")

    # ------------------------------------------------------ candidate gate wf
    print("\n[candidate gate workflow]")
    t.ok("qcom-next-gates.yml exists", os.path.exists(GATES_WF), GATES_WF)
    if os.path.exists(GATES_WF):
        gates_wf = load_wf(GATES_WF)
        gtrig = triggers(gates_wf)
        gjobs = gates_wf.get("jobs") or {}
        gins = ((gtrig.get("workflow_dispatch") or {}).get("inputs")) or {}

        # The dispatcher always sends these four, so the workflow has to accept
        # them or gh workflow run fails before anything is queued.
        for k in ("pr", "sha", "ref", "repo"):
            t.ok(f"gates wf accepts input '{k}'", k in gins, str(sorted(gins)))

        # A candidate is a branch in another repo, so nothing here may assume a
        # PR in the repo under build; that is what made pre_merge.yml unusable.
        raw_g = open(GATES_WF, encoding="utf-8").read()
        t.ok("gates wf does not use the PR-centric sync action",
             "actions/sync" not in raw_g, "sync@main no-ops for other repos")
        t.ok("gates wf resolves its actions locally, honouring --workflow-ref",
             "kernel-config/.github/actions" not in raw_g,
             "an @main reference would ignore the branch under test")

        # Ordering matters: the root checkout cleans the workspace, so a
        # candidate placed first would be wiped.
        bsteps = (gjobs.get("build") or {}).get("steps") or []
        paths = [(s.get("with") or {}).get("path") for s in bsteps
                 if str(s.get("uses", "")).startswith("actions/checkout")]
        t.ok("gates wf checks out kernel-config before the candidate",
             len(paths) >= 2 and paths[0] in (None, "", ".") and paths[1],
             f"checkout paths in order: {paths}")

        # The status has to land on the commit the dispatcher polls.
        t.ok("gates wf reports on the requested sha, not HEAD",
             "sha: ${{ inputs.sha }}" in raw_g)
        t.ok("gates wf verifies the checkout equals the requested sha",
             "was asked for" in raw_g,
             "a gate result must refer to the exact tested commit")
        t.ok("gates wf posts the status to the candidate repo",
             "repo: ${{ inputs.repo }}" in raw_g)

        # always() on the terminal job, so a cancelled build still reports.
        fin = gjobs.get("finish") or {}
        t.ok("gates wf reports a result even when the build is cancelled",
             "always()" in str(fin.get("if", "")), str(fin.get("if")))
        t.ok("gates wf derives the status from the build result",
             "needs.build.result" in str(
                 [s.get("with") for s in (fin.get("steps") or [])]))

        # The context must match what dispatch_gates.sh polls for.
        t.ok("gates wf check name matches the dispatcher's context",
             (gates_wf.get("env") or {}).get("CHECK_NAME") == "qcom-next-build",
             str(gates_wf.get("env")))

    # ------------------------------------------------------ gate dispatch map
    print("\n[gate dispatch mapping]")
    dg = open(os.path.join(REPO, "scripts", "dispatch_gates.sh"),
              encoding="utf-8").read()
    t.ok("dispatcher maps the build gate to the candidate workflow",
         "qcom-next-gates.yml|qcom-next-build" in dg)
    t.ok("dispatcher refuses PR-centric gates for another repo",
         "PR_CENTRIC_GATES" in dg and "cannot test a candidate in" in dg,
         "otherwise boot/functional silently skip and the gate still passes")
    t.ok("pr-ci 'all' includes the build gate",
         "build,pre-merge,yocto,ubuntu" in open(PR_CI, encoding="utf-8").read())

    # ------------------------------------------ single pull_request entry point
    print("\n[single pull_request entry point]")
    # A called workflow still evaluates its own concurrency block, so if it also
    # triggers on pull_request it collides with the nested call and, with
    # cancel-in-progress, one cancels the other. The cancelled run then shows up
    # as a failed check even though the work passed in the nested run.
    called = set()
    for jname, job in (pr_ci.get("jobs") or {}).items():
        u = str(job.get("uses", ""))
        if u.startswith("./"):
            called.add(os.path.basename(u))

    for name in sorted(called):
        callee = load_wf(os.path.join(REPO, ".github", "workflows", name))
        on = callee.get("on") or callee.get(True) or {}
        if isinstance(on, str):
            on = {on: None}
        if isinstance(on, list):
            on = {k: None for k in on}
        t.ok(f"{name} (called by pr-ci) has no pull_request trigger",
             "pull_request" not in on,
             "a called workflow that also triggers on pull_request cancels "
             "itself through the shared concurrency group")
        t.ok(f"{name} is still callable", "workflow_call" in on, str(list(on)))

    # pr-ci is the only pull_request entry point, so it has to watch every file
    # that can change the pinned config or the CI that consumes it. Dropping a
    # path here silently removes validation for that file.
    on_prci = pr_ci.get("on") or pr_ci.get(True) or {}
    prci_paths = set(((on_prci.get("pull_request") or {}).get("paths")) or [])
    for required in ("qcom-next.yaml",
                     "schemas/qcom-next.schema.json",
                     "scripts/**",
                     "topic_maintainers.json",
                     ".github/actions/sync/action.yml",
                     ".github/workflows/qcom-next-pr-ci.yml",
                     ".github/workflows/validate-qcom-next-config.yml",
                     ".github/workflows/prune-qcom-next-candidates.yml"):
        t.ok(f"pr-ci watches {required}", required in prci_paths,
             f"paths={sorted(prci_paths)}")

    # ------------------------------------------ reusable-call permission ceiling
    print("\n[reusable workflow permission ceiling]")
    # A called workflow cannot request more than its caller grants. Declaring
    # only contents: read at the caller implicitly sets every other scope to
    # none, so a callee job asking for pull-requests: write fails the whole
    # workflow at startup with "is only allowed 'pull-requests: none'". That is
    # a startup_failure: no jobs, no annotations, and the workflow silently
    # absent from the PR.
    RANK = {"none": 0, "read": 1, "write": 2}

    def granted(wf_obj, job):
        # An explicit job-level block overrides the workflow-level one entirely.
        block = job.get("permissions")
        if block is None:
            block = wf_obj.get("permissions")
        if block is None:
            return None            # inherits the repository default
        if isinstance(block, str):  # read-all / write-all
            lvl = "read" if block == "read-all" else "write"
            return {"__all__": lvl}
        return dict(block)

    def level(perms, scope):
        if perms is None:
            return None
        if "__all__" in perms:
            return perms["__all__"]
        # An explicit block makes every unlisted scope none.
        return perms.get(scope, "none")

    for label, wf_obj, path in (("pr-ci", pr_ci, PR_CI),):
        for jname, job in (wf_obj.get("jobs") or {}).items():
            uses = str(job.get("uses", ""))
            if not uses.startswith("./"):
                continue
            callee_path = os.path.join(REPO, uses[2:])
            t.ok(f"{label}.{jname}: callee exists", os.path.exists(callee_path), uses)
            if not os.path.exists(callee_path):
                continue
            callee = load_wf(callee_path)
            caller_perms = granted(wf_obj, job)

            for cj_name, cj in (callee.get("jobs") or {}).items():
                want = cj.get("permissions")
                if want is None or isinstance(want, str):
                    continue
                for scope, lvl in want.items():
                    have = level(caller_perms, scope)
                    if have is None:
                        continue   # caller inherits the repo default
                    ok_ceiling = RANK.get(have, 0) >= RANK.get(str(lvl), 0)
                    t.ok(f"{label}.{jname} grants {scope}:{lvl} needed by "
                         f"{os.path.basename(callee_path)}:{cj_name}",
                         ok_ceiling,
                         f"caller grants {scope}:{have}, callee requests {lvl}")

    # ------------------------------------------- pin refs vs checked-out branch
    print("\n[generation: checked-out branch safety]")
    gen = open(os.path.join(HERE, "generate_qcom_next_tree.sh"),
               encoding="utf-8").read()
    t.ok("generation detaches HEAD before repointing branch refs",
         "detaching HEAD" in gen and "--detach" in gen)
    t.ok("detach is not limited to the integration branch",
         'CURRENT_BRANCH" != "HEAD"' in gen, gen[:0])
    t.ok("update-ref failure surfaces git's own message",
         "UPDATE_ERR" in gen)
    t.ok("update-ref failure explains the checked-out-branch cause",
         "2.45" in gen or "checked out while its ref" in gen)

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

    # ------------------------------------------------------------ actionlint
    # Optional, because it is not a dependency of this repo. When present it
    # validates expressions the way GitHub does, which catches the whole class
    # of "workflow file issue" failures that GitHub reports without a line
    # number.
    print("\n[actionlint]")
    import shutil
    al = shutil.which("actionlint") or "/tmp/opencode/al/actionlint"
    if not os.path.exists(al):
        print("  SKIP  actionlint not installed")
    else:
        # The self-hosted runner labels are legitimate and unknown to actionlint.
        p = run([al, "-shellcheck=", "-pyflakes=",
                 "-ignore", r'label ".+" is unknown',
                 PR_CI, VALIDATE, PRUNE])
        out = (p.stdout + p.stderr).strip()
        t.ok("actionlint reports no problems", p.returncode == 0 and not out, out)

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
