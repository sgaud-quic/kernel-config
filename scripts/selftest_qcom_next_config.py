#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Self-tests for the qcom-next config validator and change classifier.

Runs entirely offline against synthetic configs in a temporary directory, so it
is safe in CI and on a developer machine with no network. Covers every rule the
two scripts implement, in both the accept and reject direction.

Usage::

    scripts/selftest_qcom_next_config.py          # run all tests
    scripts/selftest_qcom_next_config.py -v       # show per-case detail

Exit status: 0 all tests passed, 1 one or more failed.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
VALIDATOR = os.path.join(HERE, "validate_qcom_next_config.py")
CLASSIFIER = os.path.join(HERE, "classify_qcom_next_change.py")
SCHEMA = os.path.join(REPO, "schemas", "qcom-next.schema.json")

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40

BASELINE_CONFIG: dict = {
    "schema_version": 1,
    "base": {
        "repo": "https://github.com/qualcomm-linux/kernel.git",
        "branch": "qcom-next-staging",
        "tag": "v7.3-rc6",
        "sha": SHA_A,
    },
    "promotion_state": "open",
    "rerere_cache": {
        "repo": "https://github.com/sgaud-quic/cache",
        "ref": "master",
    },
    "topics": [
        {
            "name": "tech/bsp/clk",
            "repo": "https://github.com/qualcomm-linux/kernel-topics.git",
            "branch": "tech/bsp/clk",
            "sha": SHA_B,
            "tag": "tech/bsp/clk-7.3-rc6-20261001",
            "depends_on": [],
        },
        {
            "name": "tech/bsp/interconnect",
            "repo": "https://github.com/qualcomm-linux/kernel-topics.git",
            "branch": "tech/bsp/interconnect",
            "sha": SHA_C,
            "tag": "tech/bsp/interconnect-7.3-rc6-20261001",
            "depends_on": ["tech/bsp/clk"],
        },
        {
            "name": "tech/mm/drm",
            "repo": "https://github.com/qualcomm-linux/kernel-topics.git",
            "branch": "tech/mm/drm",
            "sha": SHA_D,
            "tag": "tech/mm/drm-7.3-rc6-20261001",
            "depends_on": [],
        },
    ],
}


# ---------------------------------------------------------------------------
class Runner:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.passed = 0
        self.failed: List[str] = []
        self.tmp = tempfile.mkdtemp(prefix="qcom-next-selftest-")

    # --- helpers ---------------------------------------------------------
    def write_cfg(self, name: str, cfg: dict) -> str:
        import yaml
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(cfg, fh, default_flow_style=False, sort_keys=False)
        return path

    def write_raw(self, name: str, text: str) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def run_validator(self, cfg_path: str, *extra: str,
                      maintainers: Optional[str] = None) -> Tuple[int, str]:
        cmd = [sys.executable, VALIDATOR, "--config", cfg_path,
               "--schema", SCHEMA, "--offline"]
        if maintainers is None:
            cmd.append("--no-maintainer-check")
        else:
            cmd += ["--maintainers", maintainers]
        cmd += list(extra)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return p.returncode, p.stdout + p.stderr

    def run_classifier(self, base: dict, head: dict, *extra: str) -> Tuple[int, dict]:
        bp = self.write_cfg("base.yaml", base)
        hp = self.write_cfg("head.yaml", head)
        cmd = [sys.executable, CLASSIFIER, "--base-file", bp, "--head-file", hp,
               "--format", "json", *extra]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        try:
            data = json.loads(p.stdout)
        except json.JSONDecodeError:
            data = {"_stdout": p.stdout, "_stderr": p.stderr}
        return p.returncode, data

    # --- assertions ------------------------------------------------------
    def check(self, label: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            if self.verbose:
                print(f"  PASS  {label}")
        else:
            self.failed.append(label)
            print(f"  FAIL  {label}")
            if detail:
                for line in detail.strip().splitlines()[:14]:
                    print(f"          {line}")

    # --- validator tests -------------------------------------------------
    def test_validator(self) -> None:
        print("\n[validator] structural rules (offline)")

        rc, out = self.run_validator(self.write_cfg("ok.yaml", BASELINE_CONFIG))
        self.check("baseline config passes", rc == 0 and "RESULT: PASS" in out, out)

        # V1: unsupported schema_version
        cfg = copy.deepcopy(BASELINE_CONFIG)
        cfg["schema_version"] = 99
        rc, out = self.run_validator(self.write_cfg("v1a.yaml", cfg))
        self.check("V1 rejects unsupported schema_version", rc == 1 and "V1" in out, out)

        # V1: missing required field
        cfg = copy.deepcopy(BASELINE_CONFIG)
        del cfg["promotion_state"]
        rc, out = self.run_validator(self.write_cfg("v1b.yaml", cfg))
        self.check("V1 rejects missing promotion_state", rc == 1 and "V1" in out, out)

        # V1: abbreviated sha
        cfg = copy.deepcopy(BASELINE_CONFIG)
        cfg["topics"][0]["sha"] = "cbb26e5"
        rc, out = self.run_validator(self.write_cfg("v1c.yaml", cfg))
        self.check("V1 rejects abbreviated sha", rc == 1 and "V1" in out, out)

        # V1: uppercase sha
        cfg = copy.deepcopy(BASELINE_CONFIG)
        cfg["topics"][0]["sha"] = "A" * 40
        rc, out = self.run_validator(self.write_cfg("v1d.yaml", cfg))
        self.check("V1 rejects uppercase sha", rc == 1 and "V1" in out, out)

        # V1: owners must not live in the config (decision 2)
        cfg = copy.deepcopy(BASELINE_CONFIG)
        cfg["topics"][0]["owners"] = ["someone"]
        rc, out = self.run_validator(self.write_cfg("v1e.yaml", cfg))
        self.check("V1 rejects owners field in topic", rc == 1 and "V1" in out, out)

        # V1: invalid branch name
        cfg = copy.deepcopy(BASELINE_CONFIG)
        cfg["topics"][0]["branch"] = "bad//branch"
        rc, out = self.run_validator(self.write_cfg("v1f.yaml", cfg))
        self.check("V1 rejects malformed ref name", rc == 1 and "V1" in out, out)

        # V1: malformed YAML
        rc, out = self.run_validator(
            self.write_raw("v1g.yaml", "schema_version: 1\n  bad: [indent\n")
        )
        self.check("V1 reports malformed YAML as usage error", rc == 2, out)

        # V2: duplicate topic name
        cfg = copy.deepcopy(BASELINE_CONFIG)
        cfg["topics"].append(copy.deepcopy(cfg["topics"][0]))
        rc, out = self.run_validator(self.write_cfg("v2a.yaml", cfg))
        self.check("V2 rejects duplicate topic name", rc == 1 and "V2" in out, out)

        # V2: duplicate repo/branch under a different name
        cfg = copy.deepcopy(BASELINE_CONFIG)
        dup = copy.deepcopy(cfg["topics"][0])
        dup["name"] = "tech/bsp/clk-copy"
        cfg["topics"].append(dup)
        rc, out = self.run_validator(self.write_cfg("v2b.yaml", cfg))
        self.check("V2 rejects duplicate repo/branch pair", rc == 1 and "V2" in out, out)

        # V8: unknown dependency
        cfg = copy.deepcopy(BASELINE_CONFIG)
        cfg["topics"][0]["depends_on"] = ["tech/does/not/exist"]
        rc, out = self.run_validator(self.write_cfg("v8a.yaml", cfg))
        self.check("V8 rejects unknown dependency", rc == 1 and "V8" in out, out)

        # V8: merge order violation
        cfg = copy.deepcopy(BASELINE_CONFIG)
        cfg["topics"][0]["depends_on"] = ["tech/mm/drm"]   # drm is later in the list
        rc, out = self.run_validator(self.write_cfg("v8b.yaml", cfg))
        self.check("V8 rejects dependency ordered after its consumer",
                   rc == 1 and "merge order violation" in out, out)

        # V8: self dependency
        cfg = copy.deepcopy(BASELINE_CONFIG)
        cfg["topics"][0]["depends_on"] = ["tech/bsp/clk"]
        rc, out = self.run_validator(self.write_cfg("v8c.yaml", cfg))
        self.check("V8 rejects self dependency", rc == 1 and "depends on itself" in out, out)

        # V8: cycle
        cfg = copy.deepcopy(BASELINE_CONFIG)
        cfg["topics"][0]["depends_on"] = ["tech/mm/drm"]
        cfg["topics"][2]["depends_on"] = ["tech/bsp/clk"]
        rc, out = self.run_validator(self.write_cfg("v8d.yaml", cfg))
        self.check("V8 detects dependency cycle", rc == 1 and "cycle" in out, out)

        # V8: valid dependency in correct order passes
        rc, out = self.run_validator(self.write_cfg("v8e.yaml", BASELINE_CONFIG))
        self.check("V8 accepts correctly ordered dependency", rc == 0, out)

        # V3 offline: null tag tolerated offline, remote rules are skipped
        cfg = copy.deepcopy(BASELINE_CONFIG)
        cfg["topics"][0]["tag"] = None
        rc, out = self.run_validator(self.write_cfg("v3a.yaml", cfg))
        self.check("null tag passes offline (remote rules skipped)", rc == 0, out)

        # OWNERS: a topic absent from topic_maintainers.json warns
        partial = self.write_raw(
            "maint_partial.json",
            json.dumps({"tech/bsp/clk": ["kernel-tech-bsp-clk-maint"]}, indent=2),
        )
        rc, out = self.run_validator(self.write_cfg("own.yaml", BASELINE_CONFIG),
                                     maintainers=partial)
        self.check("OWNERS warns for topic with no maintainer entry",
                   rc == 0 and "no entry in topic_maintainers.json" in out, out)

        # --strict promotes those warnings to errors
        rc, out = self.run_validator(self.write_cfg("own2.yaml", BASELINE_CONFIG),
                                     "--strict", maintainers=partial)
        self.check("--strict turns warnings into errors",
                   rc == 1 and "(--strict)" in out, out)

        # V5: a topic without a concrete sha is invalid (FR-INT-03). Rejected by
        # the schema and reported by V5 in the validator's own vocabulary.
        cfg = copy.deepcopy(BASELINE_CONFIG)
        cfg["topics"][1]["sha"] = None
        path = self.write_cfg("v5a.yaml", cfg)
        rc, out = self.run_validator(path)
        self.check("unpinned topic is an error, not a warning", rc == 1, out)
        self.check("unpinned topic is reported offline",
                   "sha is null" in out and "tech/bsp/interconnect" in out, out)
        self.check("unpinned topic cites FR-INT-03", "FR-INT-03" in out, out)
        self.check("message rules out a branch-tip fallback",
                   "not fall back to the branch tip" in out, out)
        self.check("schema also rejects a null topic sha",
                   "is not of type 'string'" in out, out)

        # A null base.sha is equally invalid: there is no tree without a base.
        cfg = copy.deepcopy(BASELINE_CONFIG)
        cfg["base"]["sha"] = None
        rc, out = self.run_validator(self.write_cfg("v5b.yaml", cfg))
        self.check("null base.sha is an error even offline",
                   rc == 1 and "base.sha is null" in out, out)

        # The rule must hold in online mode too, and must not be downgraded by
        # the migration flag, which only concerns tags.
        cfg = copy.deepcopy(BASELINE_CONFIG)
        cfg["topics"][0]["sha"] = None
        rc, out = self.run_validator(self.write_cfg("v5c.yaml", cfg),
                                     "--allow-untagged")
        self.check("--allow-untagged does not excuse an unpinned topic",
                   rc == 1, out)

        # markdown output renders
        rc, out = self.run_validator(self.write_cfg("md.yaml", BASELINE_CONFIG),
                                     "--format", "markdown")
        self.check("markdown output renders", rc == 0 and "qcom-next config validation" in out, out)

        # github annotations emitted on failure
        cfg = copy.deepcopy(BASELINE_CONFIG)
        cfg["topics"].append(copy.deepcopy(cfg["topics"][0]))
        rc, out = self.run_validator(self.write_cfg("gh.yaml", cfg), "--format", "github")
        self.check("github format emits ::error annotation",
                   rc == 1 and "::error" in out, out)

    # --- classifier tests ------------------------------------------------
    def test_classifier(self) -> None:
        print("\n[classifier] V6 scope and V7 freeze")

        base = BASELINE_CONFIG

        # no change
        rc, d = self.run_classifier(base, base)
        self.check("no-change detected", rc == 0 and d.get("classification") == "no-change", json.dumps(d))

        # single topic promotion
        head = copy.deepcopy(base)
        head["topics"][0]["sha"] = "1" * 40
        head["topics"][0]["tag"] = "tech/bsp/clk-7.3-rc6-20261015"
        rc, d = self.run_classifier(base, head)
        self.check("topic-promotion for one topic",
                   rc == 0 and d.get("classification") == "topic-promotion"
                   and d.get("promoted_topics") == ["tech/bsp/clk"], json.dumps(d))
        self.check("topic-promotion allows publication", d.get("publish_allowed") is True, json.dumps(d))

        # rc uprev
        head = copy.deepcopy(base)
        head["base"]["tag"] = "v7.3-rc7"
        head["base"]["sha"] = "2" * 40
        rc, d = self.run_classifier(base, head)
        self.check("rc-uprev when only base changes",
                   rc == 0 and d.get("classification") == "rc-uprev", json.dumps(d))

        # V6 mixed change rejected
        head = copy.deepcopy(base)
        head["base"]["sha"] = "2" * 40
        head["topics"][0]["sha"] = "1" * 40
        rc, d = self.run_classifier(base, head)
        self.check("V6 rejects base+topic in one PR",
                   rc == 1 and d.get("classification") == "invalid-mixed-change", json.dumps(d))

        # unjustified multi-topic promotion rejected
        head = copy.deepcopy(base)
        head["topics"][0]["sha"] = "1" * 40     # clk
        head["topics"][2]["sha"] = "3" * 40     # drm, unrelated
        rc, d = self.run_classifier(base, head)
        self.check("multi-topic without depends_on rejected",
                   rc == 1 and d.get("classification") == "invalid-mixed-change", json.dumps(d))

        # justified atomic promotion accepted (clk <- interconnect)
        head = copy.deepcopy(base)
        head["topics"][0]["sha"] = "1" * 40     # clk
        head["topics"][1]["sha"] = "4" * 40     # interconnect depends_on clk
        rc, d = self.run_classifier(base, head)
        self.check("atomic promotion justified by depends_on",
                   rc == 0 and d.get("classification") == "atomic-topic-promotion", json.dumps(d))

        # override allows unjustified atomic
        head = copy.deepcopy(base)
        head["topics"][0]["sha"] = "1" * 40
        head["topics"][2]["sha"] = "3" * 40
        rc, d = self.run_classifier(base, head, "--allow-atomic-override")
        self.check("integration-owner override allows unjustified atomic",
                   rc == 0 and d.get("classification") == "atomic-topic-promotion", json.dumps(d))

        # rerere-only
        head = copy.deepcopy(base)
        head["rerere_cache"]["ref"] = "updated-branch"
        rc, d = self.run_classifier(base, head)
        self.check("rerere-only classification",
                   rc == 0 and d.get("classification") == "rerere-only", json.dumps(d))

        # metadata-only
        head = copy.deepcopy(base)
        head["topics"][2]["comment"] = "owned by the display team"
        rc, d = self.run_classifier(base, head)
        self.check("metadata-only classification",
                   rc == 0 and d.get("classification") == "metadata-only", json.dumps(d))

        # metadata-only must not require full gates
        self.check("metadata-only requires validation only",
                   "validation only" in (d.get("required_gates") or ""), json.dumps(d))

        # repo/branch redefinition rejected
        head = copy.deepcopy(base)
        head["topics"][0]["branch"] = "tech/bsp/clk-v2"
        rc, d = self.run_classifier(base, head)
        self.check("topic branch redefinition rejected",
                   rc == 1 and d.get("classification") == "invalid-mixed-change", json.dumps(d))

        # V7: frozen base blocks promotion
        frozen = copy.deepcopy(base)
        frozen["promotion_state"] = "frozen"
        head = copy.deepcopy(frozen)
        head["topics"][0]["sha"] = "1" * 40
        rc, d = self.run_classifier(frozen, head)
        self.check("V7 blocks promotion while frozen",
                   rc == 1 and d.get("publish_allowed") is False, json.dumps(d))
        self.check("V7 still classifies the frozen change",
                   d.get("classification") == "topic-promotion", json.dumps(d))

        # V7: --ignore-freeze lets validation proceed
        rc, d = self.run_classifier(frozen, head, "--ignore-freeze")
        self.check("--ignore-freeze allows the run to continue", rc == 0, json.dumps(d))

        # V7: RC uprev may freeze
        head = copy.deepcopy(base)
        head["promotion_state"] = "frozen"
        head["base"]["sha"] = "2" * 40
        head["base"]["tag"] = "v7.3-rc7"
        rc, d = self.run_classifier(base, head)
        self.check("RC uprev may set frozen",
                   rc == 0 and d.get("classification") == "rc-uprev", json.dumps(d))

        # topic added
        head = copy.deepcopy(base)
        head["topics"].append({
            "name": "tech/net/eth",
            "repo": "https://github.com/qualcomm-linux/kernel-topics.git",
            "branch": "tech/net/eth",
            "sha": None,
            "tag": None,
            "depends_on": [],
        })
        rc, d = self.run_classifier(base, head)
        self.check("adding a topic is a promotion-class change",
                   rc == 0 and d.get("added_topics") == ["tech/net/eth"]
                   and d.get("classification") in ("topic-promotion", "atomic-topic-promotion"),
                   json.dumps(d))

        # topic removed
        head = copy.deepcopy(base)
        head["topics"] = [t for t in head["topics"] if t["name"] != "tech/mm/drm"]
        rc, d = self.run_classifier(base, head)
        self.check("removing a topic is a promotion-class change",
                   rc == 0 and d.get("removed_topics") == ["tech/mm/drm"], json.dumps(d))

        # merge order change requires full gates
        head = copy.deepcopy(base)
        head["topics"] = [head["topics"][1], head["topics"][0], head["topics"][2]]
        head["topics"][1]["depends_on"] = []
        head["topics"][0]["depends_on"] = []
        rc, d = self.run_classifier(base, head)
        self.check("merge order change is not metadata-only",
                   d.get("classification") != "metadata-only", json.dumps(d))

        # markdown rendering
        bp = self.write_cfg("base.yaml", base)
        head = copy.deepcopy(base)
        head["topics"][0]["sha"] = "1" * 40
        hp = self.write_cfg("head.yaml", head)
        p = subprocess.run(
            [sys.executable, CLASSIFIER, "--base-file", bp, "--head-file", hp,
             "--format", "markdown"],
            capture_output=True, text=True, timeout=120,
        )
        self.check("classifier markdown renders",
                   p.returncode == 0 and "classification" in p.stdout, p.stdout + p.stderr)

        # github output file
        gh = os.path.join(self.tmp, "gh_out.txt")
        p = subprocess.run(
            [sys.executable, CLASSIFIER, "--base-file", bp, "--head-file", hp,
             "--format", "text", "--github-output", gh],
            capture_output=True, text=True, timeout=120,
        )
        content = open(gh, encoding="utf-8").read() if os.path.exists(gh) else ""
        self.check("classifier writes github outputs",
                   "classification=topic-promotion" in content, content)

        # missing base argument is a usage error
        p = subprocess.run(
            [sys.executable, CLASSIFIER, "--head-file", hp],
            capture_output=True, text=True, timeout=120,
        )
        self.check("classifier requires a base", p.returncode == 2, p.stdout + p.stderr)

    # --- git-backed classifier test --------------------------------------
    def test_classifier_git(self) -> None:
        print("\n[classifier] base config read from a git ref")
        import yaml

        repo = os.path.join(self.tmp, "gitrepo")
        os.makedirs(repo, exist_ok=True)

        def git(*a: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git", "-C", repo, *a],
                                  capture_output=True, text=True, timeout=120)

        git("init", "-q")
        git("config", "user.email", "selftest@example.com")
        git("config", "user.name", "selftest")
        git("config", "commit.gpgsign", "false")

        cfg_path = os.path.join(repo, "qcom-next.yaml")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(BASELINE_CONFIG, fh, default_flow_style=False, sort_keys=False)
        git("add", "qcom-next.yaml")
        r = git("commit", "-q", "-m", "base config")
        if r.returncode != 0:
            self.check("git fixture commits", False, r.stdout + r.stderr)
            return

        head = copy.deepcopy(BASELINE_CONFIG)
        head["topics"][0]["sha"] = "1" * 40
        head["topics"][0]["tag"] = "tech/bsp/clk-7.3-rc6-20261015"
        with open(cfg_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(head, fh, default_flow_style=False, sort_keys=False)

        p = subprocess.run(
            [sys.executable, CLASSIFIER, "--base-ref", "HEAD",
             "--config", "qcom-next.yaml", "--repo-dir", repo, "--format", "json"],
            capture_output=True, text=True, timeout=120,
        )
        try:
            d = json.loads(p.stdout)
        except json.JSONDecodeError:
            d = {}
        self.check("classifies against a git ref",
                   p.returncode == 0 and d.get("classification") == "topic-promotion",
                   p.stdout + p.stderr)

        p = subprocess.run(
            [sys.executable, CLASSIFIER, "--base-ref", "does-not-exist",
             "--config", "qcom-next.yaml", "--repo-dir", repo],
            capture_output=True, text=True, timeout=120,
        )
        self.check("unknown git ref is a usage error", p.returncode == 2, p.stdout + p.stderr)

        # The config not existing at the base revision is the normal state for
        # the change that first introduces it, and must not be an error.
        empty = os.path.join(self.tmp, "gitrepo-empty")
        os.makedirs(empty, exist_ok=True)
        subprocess.run(["git", "-C", empty, "init", "-q"], timeout=60)
        for k, v in (("user.email", "selftest@example.com"),
                     ("user.name", "selftest"),
                     ("commit.gpgsign", "false")):
            subprocess.run(["git", "-C", empty, "config", k, v], timeout=60)
        with open(os.path.join(empty, "README"), "w", encoding="utf-8") as fh:
            fh.write("no config here yet\n")
        subprocess.run(["git", "-C", empty, "add", "README"], timeout=60)
        r = subprocess.run(["git", "-C", empty, "commit", "-q", "-m", "pre-config"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            self.check("initial-import fixture commits", False, r.stdout + r.stderr)
        else:
            base_rev = subprocess.run(
                ["git", "-C", empty, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=60).stdout.strip()
            with open(os.path.join(empty, "qcom-next.yaml"), "w", encoding="utf-8") as fh:
                yaml.safe_dump(BASELINE_CONFIG, fh, default_flow_style=False,
                               sort_keys=False)
            p = subprocess.run(
                [sys.executable, CLASSIFIER, "--base-ref", base_rev,
                 "--config", "qcom-next.yaml", "--repo-dir", empty,
                 "--format", "json"],
                capture_output=True, text=True, timeout=120,
            )
            try:
                d = json.loads(p.stdout)
            except json.JSONDecodeError:
                d = {}
            self.check("absent base config is not an error",
                       p.returncode == 0, p.stdout + p.stderr)
            self.check("absent base config classifies as initial-import",
                       d.get("classification") == "initial-import", json.dumps(d))
            self.check("initial-import requires the full gates",
                       "full" in (d.get("required_gates") or ""), json.dumps(d))
            self.check("initial-import lists every topic as added",
                       len(d.get("added_topics") or []) == len(BASELINE_CONFIG["topics"]),
                       json.dumps(d))
            self.check("initial-import is not reported as invalid-mixed-change",
                       d.get("classification") != "invalid-mixed-change", json.dumps(d))
            self.check("initial-import allows publication",
                       d.get("publish_allowed") is True, json.dumps(d))

    # --- real repo config -------------------------------------------------
    def test_real_config(self) -> None:
        print("\n[integration] the in-tree qcom-next.yaml")
        real = os.path.join(REPO, "qcom-next.yaml")
        if not os.path.exists(real):
            print("  SKIP  qcom-next.yaml not present")
            return
        rc, out = self.run_validator(real, "--allow-untagged")
        self.check("in-tree qcom-next.yaml passes offline validation",
                   rc == 0, out)

        rc, out = self.run_validator(real)
        self.check("in-tree config is structurally sound without --allow-untagged "
                   "(offline mode does not check tags)", rc == 0, out)

    # --- bootstrap topic_SHA1 parsing ------------------------------------
    def test_bootstrap_topic_sha1(self) -> None:
        print("\n[bootstrap] topic_SHA1 table parsing")
        bootstrap = os.path.join(HERE, "bootstrap_qcom_next_yaml.py")
        if not os.path.exists(bootstrap):
            print("  SKIP  bootstrap script not present")
            return

        sys.path.insert(0, HERE)
        try:
            import importlib.util
            # Loading the module would otherwise drop a __pycache__ directory
            # into scripts/, which is not something a test should leave behind
            # in a tree that has no .gitignore.
            prev = sys.dont_write_bytecode
            sys.dont_write_bytecode = True
            try:
                spec = importlib.util.spec_from_file_location("bootstrap_mod", bootstrap)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            finally:
                sys.dont_write_bytecode = prev
        except Exception as exc:  # noqa: BLE001
            self.check("bootstrap module imports", False, str(exc))
            return
        self.check("bootstrap module imports", True)

        # The real table is fixed-width with a header and a rule line, and at
        # least one row is tab separated rather than space padded.
        table = (
            "Name                                  SHA                                  Commits\n"
            "------------------------------------------------------------------------------------\n"
            "tech/bsp/clk              " + "b" * 40 + "         14\n"
            "tech/bsp/devfreq          " + "c" * 40 + "          7\n"
            "tech/mem/dma-buf\t\t  " + "d" * 40 + "\t\t\t1\n"
        )
        path = self.write_raw("topic_SHA1", table)
        parsed = mod.load_topic_sha1(path)

        self.check("parses three data rows", len(parsed) == 3, str(parsed))
        self.check("skips the header and rule lines",
                   "Name" not in parsed and "---" not in str(list(parsed)))
        self.check("reads a space-padded row",
                   parsed.get("tech/bsp/clk", ("",))[0] == "b" * 40, str(parsed))
        self.check("reads a tab-separated row",
                   parsed.get("tech/mem/dma-buf", ("",))[0] == "d" * 40, str(parsed))
        self.check("captures the commit count",
                   parsed.get("tech/bsp/clk", ("", ""))[1] == "14", str(parsed))

        # A row whose second field is not a SHA must be ignored, not guessed at.
        bad = self.write_raw("topic_SHA1_bad",
                             "Name  SHA  Commits\n"
                             "tech/x  not-a-sha  3\n"
                             "tech/y  " + "e" * 40 + "  1\n")
        parsed = mod.load_topic_sha1(bad)
        self.check("ignores rows without a valid sha",
                   list(parsed) == ["tech/y"], str(parsed))

        # An empty or header-only table is an error, not a silent empty config.
        empty = self.write_raw("topic_SHA1_empty", "Name  SHA  Commits\n-----\n")
        try:
            mod.load_topic_sha1(empty)
            self.check("rejects a table with no data rows", False)
        except mod.BootstrapError:
            self.check("rejects a table with no data rows", True)

        # Conflicting SHAs for one topic must not be silently resolved.
        conflict = self.write_raw("topic_SHA1_conflict",
                                  "tech/z  " + "a" * 40 + "  1\n"
                                  "tech/z  " + "b" * 40 + "  2\n")
        try:
            mod.load_topic_sha1(conflict)
            self.check("rejects conflicting SHAs for one topic", False)
        except mod.BootstrapError:
            self.check("rejects conflicting SHAs for one topic", True)

        # pick_tag must not adopt another topic's tag (regression guard).
        self.check("pick_tag ignores a foreign topic's tag",
                   mod.pick_tag(["tech/other-7.3-rc6-20261001"], "tech/bsp/clk") is None)
        self.check("pick_tag accepts the topic's own tag",
                   mod.pick_tag(["tech/bsp/clk-7.3-rc6-20261001"], "tech/bsp/clk")
                   == "tech/bsp/clk-7.3-rc6-20261001")

        # --only-topic-sha1 requires the table it filters against.
        conf = self.write_raw(
            "mini.conf",
            "baseline      https://github.com/qualcomm-linux/kernel.git  qcom-next-staging\n"
            "tech/bsp/clk  https://github.com/qualcomm-linux/kernel-topics.git  tech/bsp/clk\n"
        )
        p = subprocess.run(
            [sys.executable, bootstrap, "--conf", conf,
             "--output", os.path.join(self.tmp, "out.yaml"),
             "--only-topic-sha1", "--dry-run"],
            capture_output=True, text=True, timeout=120,
        )
        self.check("--only-topic-sha1 without --topic-sha1 is rejected",
                   p.returncode == 2 and "requires --topic-sha1" in (p.stdout + p.stderr),
                   p.stdout + p.stderr)

        # The conf parser must ignore comments and locate the baseline by name
        # rather than by position, matching ci-merge.
        conf2 = self.write_raw(
            "ordered.conf",
            "# a comment\n"
            "tech/a  https://example.invalid/t.git  tech/a\n"
            "\n"
            "baseline  https://example.invalid/k.git  main\n"
            "tech/b  https://example.invalid/t.git  tech/b\n"
        )
        base, tl = mod.parse_conf(conf2, "baseline")
        self.check("baseline located by name, not position",
                   base["branch"] == "main", str(base))
        self.check("topics keep conf order around the baseline row",
                   [t["name"] for t in tl] == ["tech/a", "tech/b"], str(tl))

        dup = self.write_raw(
            "dup.conf",
            "baseline  https://example.invalid/k.git  main\n"
            "tech/a  https://example.invalid/t.git  tech/a\n"
            "tech/a  https://example.invalid/t.git  tech/a\n"
        )
        try:
            mod.parse_conf(dup, "baseline")
            self.check("duplicate conf rows are rejected", False)
        except mod.BootstrapError:
            self.check("duplicate conf rows are rejected", True)

        nobase = self.write_raw(
            "nobase.conf",
            "tech/a  https://example.invalid/t.git  tech/a\n"
        )
        try:
            mod.parse_conf(nobase, "baseline")
            self.check("conf without a baseline row is rejected", False)
        except mod.BootstrapError:
            self.check("conf without a baseline row is rejected", True)

    # --- entry ------------------------------------------------------------
    def run(self) -> int:
        print("qcom-next config self-tests")
        print(f"  validator : {VALIDATOR}")
        print(f"  classifier: {CLASSIFIER}")
        print(f"  schema    : {SCHEMA}")
        print(f"  tmpdir    : {self.tmp}")

        try:
            import yaml  # noqa: F401
            import jsonschema  # noqa: F401
        except ImportError as exc:
            print(f"\nerror: missing dependency: {exc}")
            print("install with: pip install pyyaml jsonschema")
            return 2

        self.test_validator()
        self.test_classifier()
        self.test_classifier_git()
        self.test_bootstrap_topic_sha1()
        self.test_real_config()

        total = self.passed + len(self.failed)
        print(f"\n{'=' * 60}")
        print(f"ran {total} checks: {self.passed} passed, {len(self.failed)} failed")
        if self.failed:
            print("\nfailed checks:")
            for name in self.failed:
                print(f"  - {name}")
        print("RESULT:", "PASS" if not self.failed else "FAIL")
        return 0 if not self.failed else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Self-tests for qcom-next config tooling.")
    ap.add_argument("-v", "--verbose", action="store_true", help="show passing checks")
    args = ap.parse_args(argv)
    return Runner(verbose=args.verbose).run()


if __name__ == "__main__":
    sys.exit(main())
