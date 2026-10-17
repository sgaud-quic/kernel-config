#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Validate qcom-next.yaml, the SHA-pinned source of truth for qcom-next.

This is the first CI stage for every kernel-config pull request. It enforces the
structural and data-integrity rules from the Kernel Topic Promotion Flow design:

  V1 Schema      config parses and conforms to schemas/qcom-next.schema.json
  V2 Duplicates  no duplicate topic names, no duplicate repo/branch pairs
  V3 Tagged SHA  every configured sha exists and its tag resolves to that sha
  V4 Branch      every configured topic branch still exists on its remote
  V5 New topic   a topic with sha: null resolves to the branch tip (reported so
                 it can be recorded and tagged)
  V8 Dependency  depends_on references existing topics, has no cycles, and the
                 topics array order respects the dependency graph

Scope and freeze rules V6 and V7 are diff-based: they compare a PR's config
against the target branch config and therefore live in
scripts/classify_qcom_next_change.py. Run both; this script validates a single
config in isolation.

Exit status:
  0  all checks passed (warnings may still be present)
  1  one or more validation errors
  2  usage error, unreadable input, or missing dependency

Typical use::

    scripts/validate_qcom_next_config.py                       # full, hits remotes
    scripts/validate_qcom_next_config.py --offline             # structural only
    scripts/validate_qcom_next_config.py --allow-untagged      # migration mode
    scripts/validate_qcom_next_config.py --format github       # CI annotations
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Sequence, Tuple

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SUPPORTED_SCHEMA_VERSIONS = (1,)

DEFAULT_CONFIG = "qcom-next.yaml"
DEFAULT_SCHEMA = "schemas/qcom-next.schema.json"
DEFAULT_MAINTAINERS = "topic_maintainers.json"


# ---------------------------------------------------------------------------
# finding collection
# ---------------------------------------------------------------------------
class Findings:
    """Collects rule results so every problem is reported, not just the first."""

    def __init__(self) -> None:
        self.errors: List[Tuple[str, str]] = []
        self.warnings: List[Tuple[str, str]] = []
        self.infos: List[Tuple[str, str]] = []
        self.checked: List[str] = []

    def error(self, rule: str, msg: str) -> None:
        self.errors.append((rule, msg))

    def warn(self, rule: str, msg: str) -> None:
        self.warnings.append((rule, msg))

    def info(self, rule: str, msg: str) -> None:
        self.infos.append((rule, msg))

    def ran(self, rule: str) -> None:
        if rule not in self.checked:
            self.checked.append(rule)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# remote access
# ---------------------------------------------------------------------------
class RemoteIndex:
    """Lazily caches `git ls-remote` output, one call per repository.

    A single ls-remote per repo keeps validation fast: all ~77 topics share one
    remote, so the whole config costs two network round-trips.
    """

    def __init__(self, timeout: int = 180) -> None:
        self._heads: Dict[str, Dict[str, str]] = {}
        self._tags: Dict[str, Dict[str, str]] = {}
        self._failed: Dict[str, str] = {}
        self.timeout = timeout

    def _run(self, repo: str, which: str) -> Optional[List[Tuple[str, str]]]:
        cmd = ["git", "ls-remote", which, repo]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except subprocess.TimeoutExpired:
            self._failed[repo] = f"git ls-remote {which} timed out after {self.timeout}s"
            return None
        except OSError as exc:
            self._failed[repo] = f"cannot execute git: {exc}"
            return None
        if proc.returncode != 0:
            tail = proc.stderr.strip().splitlines()
            self._failed[repo] = (
                f"git ls-remote {which} exit {proc.returncode}: "
                f"{tail[-1] if tail else 'no stderr'}"
            )
            return None
        rows: List[Tuple[str, str]] = []
        for line in proc.stdout.splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                rows.append((parts[0].strip(), parts[1].strip()))
        return rows

    def failure(self, repo: str) -> Optional[str]:
        return self._failed.get(repo)

    def heads(self, repo: str) -> Optional[Dict[str, str]]:
        if repo in self._heads:
            return self._heads[repo]
        if repo in self._failed:
            return None
        rows = self._run(repo, "--heads")
        if rows is None:
            return None
        out = {
            ref[len("refs/heads/"):]: sha
            for sha, ref in rows
            if ref.startswith("refs/heads/")
        }
        self._heads[repo] = out
        return out

    def tags(self, repo: str) -> Optional[Dict[str, str]]:
        """Return {tag_name: commit_sha}, resolving annotated tags to commits."""
        if repo in self._tags:
            return self._tags[repo]
        if repo in self._failed:
            return None
        rows = self._run(repo, "--tags")
        if rows is None:
            return None
        raw: Dict[str, str] = {}
        for sha, ref in rows:
            if ref.startswith("refs/tags/"):
                raw[ref[len("refs/tags/"):]] = sha
        out: Dict[str, str] = {}
        for name, sha in raw.items():
            if name.endswith("^{}"):
                out[name[:-3]] = sha          # peeled: the real commit
            else:
                out.setdefault(name, sha)     # lightweight, unless peeled wins
        for name, sha in raw.items():
            if name.endswith("^{}"):
                out[name[:-3]] = sha           # peeled always takes precedence
        self._tags[repo] = out
        return out


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def load_yaml(path: str):
    try:
        import yaml
    except ImportError:
        sys.stderr.write(
            "error: PyYAML is required. Install with: pip install pyyaml\n"
        )
        raise SystemExit(2)
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except FileNotFoundError:
        sys.stderr.write(f"error: config not found: {path}\n")
        raise SystemExit(2)
    except yaml.YAMLError as exc:
        sys.stderr.write(f"error: {path} is not valid YAML:\n  {exc}\n")
        raise SystemExit(2)
    except OSError as exc:
        sys.stderr.write(f"error: cannot read {path}: {exc}\n")
        raise SystemExit(2)


def load_json(path: str, what: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        sys.stderr.write(f"error: {what} not found: {path}\n")
        raise SystemExit(2)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"error: {what} is not valid JSON: {path}\n  {exc}\n")
        raise SystemExit(2)
    except OSError as exc:
        sys.stderr.write(f"error: cannot read {what} {path}: {exc}\n")
        raise SystemExit(2)


# ---------------------------------------------------------------------------
# V1 — schema
# ---------------------------------------------------------------------------
def check_schema(cfg, schema, f: Findings) -> bool:
    f.ran("V1")
    try:
        import jsonschema
    except ImportError:
        sys.stderr.write(
            "error: jsonschema is required. Install with: pip install jsonschema\n"
        )
        raise SystemExit(2)

    if not isinstance(cfg, dict):
        f.error("V1", f"top level must be a mapping, got {type(cfg).__name__}")
        return False

    validator_cls = jsonschema.validators.validator_for(schema)
    try:
        validator_cls.check_schema(schema)
    except jsonschema.SchemaError as exc:
        f.error("V1", f"schema file itself is invalid: {exc.message}")
        return False

    errors = sorted(validator_cls(schema).iter_errors(cfg),
                    key=lambda e: list(e.absolute_path))
    for err in errors:
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        f.error("V1", f"{where}: {err.message}")

    version = cfg.get("schema_version")
    if version is not None and version not in SUPPORTED_SCHEMA_VERSIONS:
        f.error(
            "V1",
            f"schema_version {version!r} is not supported by this validator "
            f"(supported: {', '.join(map(str, SUPPORTED_SCHEMA_VERSIONS))})",
        )

    return not errors


# ---------------------------------------------------------------------------
# V2 — duplicates
# ---------------------------------------------------------------------------
def check_duplicates(topics: Sequence[dict], f: Findings) -> None:
    f.ran("V2")
    by_name: Dict[str, List[int]] = {}
    by_pair: Dict[Tuple[str, str], List[int]] = {}

    for idx, topic in enumerate(topics):
        name = topic.get("name")
        if isinstance(name, str):
            by_name.setdefault(name, []).append(idx)
        repo, branch = topic.get("repo"), topic.get("branch")
        if isinstance(repo, str) and isinstance(branch, str):
            by_pair.setdefault((repo, branch), []).append(idx)

    for name, idxs in by_name.items():
        if len(idxs) > 1:
            f.error("V2", f"duplicate topic name {name!r} at topics[{'], topics['.join(map(str, idxs))}]")

    for (repo, branch), idxs in by_pair.items():
        if len(idxs) > 1:
            names = ", ".join(repr(topics[i].get("name")) for i in idxs)
            f.error(
                "V2",
                f"duplicate repo/branch pair {branch!r} in {repo} shared by {names}; "
                "the same branch must not be merged twice",
            )


# ---------------------------------------------------------------------------
# V8 — dependencies
# ---------------------------------------------------------------------------
def check_dependencies(topics: Sequence[dict], f: Findings) -> None:
    f.ran("V8")
    position = {
        t["name"]: i for i, t in enumerate(topics) if isinstance(t.get("name"), str)
    }
    graph: Dict[str, List[str]] = {}

    for idx, topic in enumerate(topics):
        name = topic.get("name")
        if not isinstance(name, str):
            continue
        deps = topic.get("depends_on") or []
        graph[name] = []
        for dep in deps:
            if dep == name:
                f.error("V8", f"topic {name!r} depends on itself")
                continue
            if dep not in position:
                f.error(
                    "V8",
                    f"topic {name!r} depends on {dep!r}, which is not a topic in this config",
                )
                continue
            graph[name].append(dep)
            if position[dep] > idx:
                f.error(
                    "V8",
                    f"merge order violation: {name!r} (position {idx}) depends on "
                    f"{dep!r} (position {position[dep]}); a dependency must be "
                    "merged before the topic that needs it",
                )

    # cycle detection, independent of array order
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {n: WHITE for n in graph}
    reported: set = set()

    def walk(node: str, trail: List[str]) -> None:
        colour[node] = GREY
        for dep in graph.get(node, []):
            if colour.get(dep) == GREY:
                cycle = trail[trail.index(dep):] + [dep] if dep in trail else [node, dep]
                key = frozenset(cycle)
                if key not in reported:
                    reported.add(key)
                    f.error("V8", "dependency cycle: " + " -> ".join(cycle))
            elif colour.get(dep) == WHITE:
                walk(dep, trail + [dep])
        colour[node] = BLACK

    for node in graph:
        if colour[node] == WHITE:
            walk(node, [node])


# ---------------------------------------------------------------------------
# V3 / V4 / V5 — remote reality checks
# ---------------------------------------------------------------------------
def check_remotes(cfg: dict, topics: Sequence[dict], index: RemoteIndex,
                  allow_untagged: bool, f: Findings) -> None:
    f.ran("V3")
    f.ran("V4")
    f.ran("V5")

    # ---- base block ----
    base = cfg.get("base") or {}
    base_repo = base.get("repo")
    base_branch = base.get("branch")
    base_sha = base.get("sha")
    base_tag = base.get("tag")

    if isinstance(base_repo, str):
        heads = index.heads(base_repo)
        if heads is None:
            f.warn("V4", f"base: cannot reach {base_repo}: {index.failure(base_repo)}")
        elif isinstance(base_branch, str) and base_branch not in heads:
            f.error("V4", f"base.branch {base_branch!r} does not exist in {base_repo}")

        if base_sha is None:
            f.error("V3", "base.sha is null; an RC uprev must pin a concrete baseline commit")
        elif isinstance(base_sha, str):
            tags = index.tags(base_repo)
            if tags is None:
                f.warn("V3", f"base: cannot list tags of {base_repo}: {index.failure(base_repo)}")
            else:
                if isinstance(base_tag, str):
                    if base_tag not in tags:
                        f.error("V3", f"base.tag {base_tag!r} does not exist in {base_repo}")
                    elif tags[base_tag] != base_sha:
                        f.error(
                            "V3",
                            f"base.tag {base_tag!r} points at {tags[base_tag][:12]} but "
                            f"base.sha is {base_sha[:12]}",
                        )
                elif base_tag is None:
                    msg = "base.tag is null; the baseline commit should be tagged for traceability"
                    (f.warn if allow_untagged else f.error)("V3", msg)

                # reachability: the pinned base sha must be referenced by some ref
                if heads is not None and base_sha not in set(heads.values()) | set(tags.values()):
                    f.warn(
                        "V3",
                        f"base.sha {base_sha[:12]} is not at the tip of any branch or tag "
                        f"in {base_repo}; it may still be reachable from history "
                        "(ls-remote only sees ref tips)",
                    )

    # ---- topics ----
    for idx, topic in enumerate(topics):
        name = topic.get("name", f"<topics[{idx}]>")
        repo = topic.get("repo")
        branch = topic.get("branch")
        sha = topic.get("sha")
        tag = topic.get("tag")

        if not isinstance(repo, str):
            continue

        heads = index.heads(repo)
        if heads is None:
            f.warn("V4", f"{name}: cannot reach {repo}: {index.failure(repo)}")
            continue

        # V4 — branch must still exist
        if isinstance(branch, str) and branch not in heads:
            f.error(
                "V4",
                f"{name}: branch {branch!r} does not exist in {repo}. If the branch was "
                "renamed or deleted, update or remove this entry (FR-INT-02)",
            )
            continue

        tip = heads.get(branch) if isinstance(branch, str) else None

        # V5 — new topic with no pinned sha
        if sha is None:
            if tip:
                f.info(
                    "V5",
                    f"{name}: sha is null (new topic); branch tip is {tip}. "
                    f"Record this sha and tag it as "
                    f"{branch}-<kernel-version>-<YYYYMMDD> before promotion",
                )
                f.warn(
                    "V5",
                    f"{name}: sha is null; the branch tip will be used. Pin and tag it "
                    "so the consumed commit is reproducible (FR-INT-03)",
                )
            continue

        if not isinstance(sha, str) or not SHA_RE.match(sha):
            continue  # already reported by V1

        # V3 — tag must exist and resolve to the pinned sha
        tags = index.tags(repo)
        if tags is None:
            f.warn("V3", f"{name}: cannot list tags of {repo}: {index.failure(repo)}")
            continue

        if tag is None:
            msg = (
                f"{name}: tag is null. Every consumed sha must be reachable from a tag "
                "so it survives a topic-branch rebase (FR-INT-01)"
            )
            (f.warn if allow_untagged else f.error)("V3", msg)
        elif not isinstance(tag, str):
            pass  # reported by V1
        elif tag not in tags:
            f.error("V3", f"{name}: tag {tag!r} does not exist in {repo}")
        elif tags[tag] != sha:
            f.error(
                "V3",
                f"{name}: tag {tag!r} points at {tags[tag][:12]} but the configured "
                f"sha is {sha[:12]}",
            )

        # informational: pinned sha behind the branch tip is normal and expected
        if tip and tip != sha:
            f.info("V3", f"{name}: pinned {sha[:12]}, branch tip is now {tip[:12]}")


# ---------------------------------------------------------------------------
# advisory — maintainer coverage
# ---------------------------------------------------------------------------
def check_maintainers(topics: Sequence[dict], maintainers: Optional[dict],
                      f: Findings) -> None:
    if maintainers is None:
        return
    f.ran("OWNERS")
    names = [t.get("name") for t in topics if isinstance(t.get("name"), str)]
    for name in names:
        if name not in maintainers:
            f.warn(
                "OWNERS",
                f"{name}: no entry in topic_maintainers.json; promotion PRs for this "
                "topic cannot auto-request a reviewer",
            )
    for key in maintainers:
        if key not in names:
            f.info("OWNERS", f"topic_maintainers.json has {key!r}, which is not a topic in this config")


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
RULE_TITLES = {
    "V1": "schema",
    "V2": "duplicates",
    "V3": "tagged SHA",
    "V4": "branch exists",
    "V5": "new topic",
    "V8": "dependencies",
    "OWNERS": "maintainer coverage",
}


def report_text(cfg, topics, f: Findings, offline: bool, path: str) -> None:
    print(f"qcom-next config validation: {path}")
    if isinstance(cfg, dict):
        base = cfg.get("base") or {}
        tagged = sum(1 for t in topics if t.get("tag"))
        print(f"  schema_version : {cfg.get('schema_version')}")
        print(f"  promotion_state: {cfg.get('promotion_state')}")
        print(f"  base           : {base.get('branch')} @ "
              f"{str(base.get('sha'))[:12]} (tag: {base.get('tag')})")
        print(f"  topics         : {len(topics)}  ({tagged} tagged, "
              f"{len(topics) - tagged} untagged)")
    print(f"  rules run      : {', '.join(f.checked) if f.checked else 'none'}")
    print(f"  mode           : {'offline (structural only)' if offline else 'online'}")

    if f.errors:
        print(f"\nERRORS ({len(f.errors)}):")
        for rule, msg in f.errors:
            print(f"  [{rule} {RULE_TITLES.get(rule, '')}] {msg}")
    if f.warnings:
        print(f"\nWARNINGS ({len(f.warnings)}):")
        for rule, msg in f.warnings:
            print(f"  [{rule} {RULE_TITLES.get(rule, '')}] {msg}")
    if f.infos:
        print(f"\nNOTES ({len(f.infos)}):")
        for rule, msg in f.infos:
            print(f"  [{rule}] {msg}")

    print()
    print("RESULT: PASS" if f.ok else f"RESULT: FAIL ({len(f.errors)} error(s))")


def report_github(f: Findings) -> None:
    """Emit GitHub Actions workflow commands so findings appear as annotations."""
    for rule, msg in f.errors:
        print(f"::error title=qcom-next config {rule}::{msg}")
    for rule, msg in f.warnings:
        print(f"::warning title=qcom-next config {rule}::{msg}")
    for rule, msg in f.infos:
        print(f"::notice title=qcom-next config {rule}::{msg}")


def report_markdown(cfg, topics, f: Findings, offline: bool, path: str) -> str:
    lines: List[str] = []
    a = lines.append
    status = "PASS" if f.ok else "FAIL"
    a(f"### qcom-next config validation: **{status}**")
    a("")
    if isinstance(cfg, dict):
        base = cfg.get("base") or {}
        tagged = sum(1 for t in topics if t.get("tag"))
        a("| Field | Value |")
        a("|---|---|")
        a(f"| Config | `{path}` |")
        a(f"| schema_version | `{cfg.get('schema_version')}` |")
        a(f"| promotion_state | `{cfg.get('promotion_state')}` |")
        a(f"| Base | `{base.get('branch')}` @ `{str(base.get('sha'))[:12]}` "
          f"(tag `{base.get('tag')}`) |")
        a(f"| Topics | {len(topics)} ({tagged} tagged, {len(topics) - tagged} untagged) |")
        a(f"| Rules run | {', '.join(f.checked)} |")
        a(f"| Mode | {'offline' if offline else 'online'} |")
        a("")
    for title, items in (("Errors", f.errors), ("Warnings", f.warnings)):
        if items:
            a(f"<details open><summary><b>{title} ({len(items)})</b></summary>")
            a("")
            for rule, msg in items:
                a(f"- `{rule}` {msg}")
            a("")
            a("</details>")
            a("")
    if f.infos:
        a(f"<details><summary>Notes ({len(f.infos)})</summary>")
        a("")
        for rule, msg in f.infos:
            a(f"- `{rule}` {msg}")
        a("")
        a("</details>")
        a("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Validate the SHA-pinned qcom-next topic configuration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit 0 = pass, 1 = validation errors, 2 = usage/dependency error.",
    )
    ap.add_argument("--config", default=DEFAULT_CONFIG,
                    help="config to validate (default: %(default)s)")
    ap.add_argument("--schema", default=DEFAULT_SCHEMA,
                    help="JSON schema path (default: %(default)s)")
    ap.add_argument("--maintainers", default=DEFAULT_MAINTAINERS,
                    help="topic owners file for advisory coverage checks "
                         "(default: %(default)s)")
    ap.add_argument("--offline", action="store_true",
                    help="skip all remote checks (V3/V4/V5); run V1/V2/V8 only")
    ap.add_argument("--allow-untagged", action="store_true",
                    help="downgrade missing-tag errors to warnings. Intended for the "
                         "migration period only; promotion PRs must run without it")
    ap.add_argument("--no-maintainer-check", action="store_true",
                    help="skip the advisory topic_maintainers.json coverage check")
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as errors")
    ap.add_argument("--format", choices=["text", "github", "markdown"], default="text",
                    help="output format (default: %(default)s)")
    ap.add_argument("--markdown-out", metavar="PATH",
                    help="also write a Markdown summary to PATH (for PR comments "
                         "or $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--timeout", type=int, default=180,
                    help="per-remote git ls-remote timeout in seconds (default: %(default)s)")
    args = ap.parse_args(argv)

    cfg = load_yaml(args.config)
    schema = load_json(args.schema, "schema")

    maintainers = None
    if not args.no_maintainer_check and os.path.exists(args.maintainers):
        maintainers = load_json(args.maintainers, "maintainers file")

    f = Findings()

    schema_ok = check_schema(cfg, schema, f)

    topics: List[dict] = []
    if isinstance(cfg, dict):
        raw = cfg.get("topics")
        if isinstance(raw, list):
            topics = [t for t in raw if isinstance(t, dict)]

    # Structural rules are safe to run even when the schema failed, and give the
    # author more to fix per iteration. Remote rules are skipped on schema
    # failure because the fields they read may be malformed.
    check_duplicates(topics, f)
    check_dependencies(topics, f)

    if args.offline:
        f.info("V3", "offline mode: remote SHA/tag/branch checks skipped")
    elif not schema_ok:
        f.warn("V3", "schema errors present; remote checks skipped until the config is well-formed")
    else:
        index = RemoteIndex(timeout=args.timeout)
        check_remotes(cfg, topics, index, args.allow_untagged, f)

    if maintainers is not None:
        check_maintainers(topics, maintainers, f)

    if args.strict and f.warnings:
        promoted = f.warnings
        f.warnings = []
        for rule, msg in promoted:
            f.error(rule, f"(--strict) {msg}")

    if args.format == "github":
        report_github(f)
        report_text(cfg, topics, f, args.offline, args.config)
    elif args.format == "markdown":
        print(report_markdown(cfg, topics, f, args.offline, args.config))
    else:
        report_text(cfg, topics, f, args.offline, args.config)

    if args.markdown_out:
        try:
            with open(args.markdown_out, "w", encoding="utf-8") as fh:
                fh.write(report_markdown(cfg, topics, f, args.offline, args.config) + "\n")
        except OSError as exc:
            sys.stderr.write(f"warning: cannot write {args.markdown_out}: {exc}\n")

    return 0 if f.ok else 1


if __name__ == "__main__":
    sys.exit(main())
