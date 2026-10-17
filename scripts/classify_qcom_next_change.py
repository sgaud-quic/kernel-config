#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Classify a qcom-next.yaml change by diffing a PR config against its base.

Runs immediately after scripts/validate_qcom_next_config.py. The validator
checks a single config in isolation; this script answers "what kind of change is
this PR?" and enforces the two diff-based rules from the design:

  V6 Scope   a promotion PR changes only topic entries; an RC uprev PR changes
             only the base block. Mixing them is rejected, because a regression
             could then not be attributed to either the upstream RC or a topic.
  V7 Freeze  while promotion_state is frozen (an RC uprev is in flight) a topic
             promotion may validate but must not publish or merge.

Classifications:

  rc-uprev                only base fields changed
  topic-promotion         exactly one topic's sha/tag changed
  atomic-topic-promotion  several topic sha/tag changes that dependency metadata
                          justifies moving together
  metadata-only           depends_on, comments or other non-published metadata
  rerere-only             only the rerere cache reference changed
  no-change               configs are identical
  invalid-mixed-change    base and topics changed together, or an unjustified
                          multi-topic change, or a frozen-state violation

The classification is written to --github-output so later jobs can gate on it.

Exit status:
  0  classified, and the change is allowed to proceed
  1  invalid-mixed-change, or blocked by the freeze rule
  2  usage error, unreadable input, or missing dependency
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Dict, List, Optional, Sequence, Set, Tuple

PUBLISHED_TOPIC_FIELDS = ("sha", "tag")
METADATA_TOPIC_FIELDS = ("depends_on", "comment")

CLASS_RC_UPREV = "rc-uprev"
CLASS_TOPIC = "topic-promotion"
CLASS_ATOMIC = "atomic-topic-promotion"
CLASS_METADATA = "metadata-only"
CLASS_RERERE = "rerere-only"
CLASS_NO_CHANGE = "no-change"
CLASS_INITIAL = "initial-import"
CLASS_INVALID = "invalid-mixed-change"


class ClassifyError(RuntimeError):
    pass


class BaseConfigAbsent(ClassifyError):
    """The config does not exist at the base revision.

    Expected for the change that first introduces qcom-next.yaml, and for a
    manual run against a branch that predates it. Distinguished from a genuine
    read failure so the caller can treat it as an initial import rather than an
    error.
    """


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def _yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        sys.stderr.write("error: PyYAML is required. Install with: pip install pyyaml\n")
        raise SystemExit(2)


def load_config_file(path: str) -> dict:
    yaml = _yaml()
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        raise ClassifyError(f"config not found: {path}")
    except yaml.YAMLError as exc:
        raise ClassifyError(f"{path} is not valid YAML: {exc}")
    except OSError as exc:
        raise ClassifyError(f"cannot read {path}: {exc}")
    if not isinstance(data, dict):
        raise ClassifyError(f"{path}: top level must be a mapping")
    return data


def _git(repo_dir: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise ClassifyError(f"git {' '.join(args)} timed out")
    except OSError as exc:
        raise ClassifyError(f"cannot execute git: {exc}")


def load_config_from_git(ref: str, path: str, repo_dir: str = ".") -> dict:
    """Read `path` at `ref` via `git show`, for comparing against a base branch.

    Existence is probed with plumbing rather than by matching git's stderr,
    which is locale dependent. An unresolvable ref is an error; a resolvable ref
    that simply has no such path raises BaseConfigAbsent, because that is the
    normal state before qcom-next.yaml is first merged.
    """
    yaml = _yaml()

    if _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        raise ClassifyError(
            f"base ref {ref!r} cannot be resolved in {repo_dir}. "
            "Fetch it first, or pass --base-file instead."
        )

    if _git(repo_dir, "cat-file", "-e", f"{ref}:{path}").returncode != 0:
        raise BaseConfigAbsent(f"{path} does not exist at {ref}")

    proc = _git(repo_dir, "show", f"{ref}:{path}")
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()
        raise ClassifyError(
            f"git show {ref}:{path} failed (exit {proc.returncode}): "
            f"{tail[-1] if tail else 'no stderr'}"
        )
    try:
        data = yaml.safe_load(proc.stdout)
    except yaml.YAMLError as exc:
        raise ClassifyError(f"{ref}:{path} is not valid YAML: {exc}")
    if not isinstance(data, dict):
        raise ClassifyError(f"{ref}:{path}: top level must be a mapping")
    return data


# ---------------------------------------------------------------------------
# diffing
# ---------------------------------------------------------------------------
def topic_map(cfg: dict) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    raw = cfg.get("topics")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                out[item["name"]] = item
    return out


def topic_order(cfg: dict) -> List[str]:
    raw = cfg.get("topics")
    if not isinstance(raw, list):
        return []
    return [t["name"] for t in raw
            if isinstance(t, dict) and isinstance(t.get("name"), str)]


class Diff:
    """Structured difference between a base config and a PR config."""

    def __init__(self, base: dict, head: dict) -> None:
        self.base = base
        self.head = head

        self.base_changes: Dict[str, Tuple] = {}
        for key in ("repo", "branch", "tag", "sha"):
            b = (base.get("base") or {}).get(key)
            h = (head.get("base") or {}).get(key)
            if b != h:
                self.base_changes[key] = (b, h)

        self.rerere_changes: Dict[str, Tuple] = {}
        for key in ("repo", "ref", "sha"):
            b = (base.get("rerere_cache") or {}).get(key)
            h = (head.get("rerere_cache") or {}).get(key)
            if b != h:
                self.rerere_changes[key] = (b, h)

        self.promotion_state_change: Optional[Tuple] = None
        if base.get("promotion_state") != head.get("promotion_state"):
            self.promotion_state_change = (
                base.get("promotion_state"), head.get("promotion_state")
            )

        self.schema_version_change: Optional[Tuple] = None
        if base.get("schema_version") != head.get("schema_version"):
            self.schema_version_change = (
                base.get("schema_version"), head.get("schema_version")
            )

        bt, ht = topic_map(base), topic_map(head)
        self.added: List[str] = [n for n in topic_order(head) if n not in bt]
        self.removed: List[str] = [n for n in topic_order(base) if n not in ht]

        # promoted: a published field (sha/tag) changed
        self.promoted: Dict[str, Dict[str, Tuple]] = {}
        # metadata: only non-published fields changed
        self.metadata: Dict[str, Dict[str, Tuple]] = {}
        # moved: repo or branch changed, which is a redefinition, not a promotion
        self.redefined: Dict[str, Dict[str, Tuple]] = {}

        for name in topic_order(head):
            if name not in bt:
                continue
            b, h = bt[name], ht[name]

            pub = {k: (b.get(k), h.get(k)) for k in PUBLISHED_TOPIC_FIELDS
                   if b.get(k) != h.get(k)}
            meta = {k: (b.get(k) or ([] if k == "depends_on" else None),
                        h.get(k) or ([] if k == "depends_on" else None))
                    for k in METADATA_TOPIC_FIELDS
                    if (b.get(k) or ([] if k == "depends_on" else None))
                    != (h.get(k) or ([] if k == "depends_on" else None))}
            redef = {k: (b.get(k), h.get(k)) for k in ("repo", "branch")
                     if b.get(k) != h.get(k)}

            if pub:
                self.promoted[name] = pub
            if meta:
                self.metadata[name] = meta
            if redef:
                self.redefined[name] = redef

        self.order_changed = (
            [n for n in topic_order(base) if n in ht]
            != [n for n in topic_order(head) if n in bt]
        )

    @property
    def touches_base(self) -> bool:
        return bool(self.base_changes)

    @property
    def touches_topics(self) -> bool:
        return bool(self.promoted or self.added or self.removed or self.redefined)

    @property
    def any_change(self) -> bool:
        return bool(
            self.base_changes or self.rerere_changes or self.promotion_state_change
            or self.schema_version_change or self.promoted or self.metadata
            or self.added or self.removed or self.redefined or self.order_changed
        )


# ---------------------------------------------------------------------------
# dependency justification for atomic promotions
# ---------------------------------------------------------------------------
def dependency_closure(names: Sequence[str], cfg: dict) -> Dict[str, Set[str]]:
    tm = topic_map(cfg)
    direct: Dict[str, Set[str]] = {}
    for name in tm:
        deps = tm[name].get("depends_on") or []
        direct[name] = {d for d in deps if isinstance(d, str)}
    return direct


def atomic_is_justified(promoted: Sequence[str], head: dict) -> Tuple[bool, str]:
    """A multi-topic promotion is justified when the promoted set is connected
    by depends_on edges, i.e. the topics genuinely need to move together."""
    if len(promoted) < 2:
        return True, "single topic"

    direct = dependency_closure(promoted, head)
    promoted_set = set(promoted)

    # Build an undirected graph restricted to the promoted set.
    adj: Dict[str, Set[str]] = {n: set() for n in promoted}
    for name in promoted:
        for dep in direct.get(name, set()):
            if dep in promoted_set:
                adj[name].add(dep)
                adj[dep].add(name)

    # Every promoted topic must participate in at least one edge.
    isolated = [n for n in promoted if not adj[n]]
    if isolated:
        return False, (
            "no depends_on relationship links "
            + ", ".join(repr(n) for n in isolated)
            + " to the rest of the promoted set"
        )

    # And the whole set must form one connected component.
    seen: Set[str] = set()
    stack = [promoted[0]]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(adj[cur] - seen)
    if seen != promoted_set:
        return False, (
            "promoted topics form more than one independent dependency group: "
            + " | ".join(
                ", ".join(sorted(g)) for g in _components(adj, promoted_set)
            )
        )
    return True, "connected by depends_on"


def _components(adj: Dict[str, Set[str]], nodes: Set[str]) -> List[Set[str]]:
    out: List[Set[str]] = []
    unseen = set(nodes)
    while unseen:
        start = next(iter(unseen))
        comp: Set[str] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur in comp:
                continue
            comp.add(cur)
            stack.extend(adj.get(cur, set()) - comp)
        out.append(comp)
        unseen -= comp
    return out


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------
def classify(diff: Diff, allow_atomic_override: bool,
             base_absent: bool = False) -> Tuple[str, List[str], List[str]]:
    """Return (classification, reasons, blocking_errors)."""
    reasons: List[str] = []
    errors: List[str] = []

    if base_absent:
        # Nothing to diff against. Every topic is new and the base block is new,
        # which the V6 scope rule below would otherwise read as a base+topic
        # change and reject. Classify it explicitly instead: the tree still has
        # to be generated and must still pass the full gates.
        topics = topic_order(diff.head)
        base = diff.head.get("base") or {}
        reasons.append(
            f"config does not exist at the base revision: initial import of "
            f"{len(topics)} topic(s)"
        )
        reasons.append(
            f"baseline {base.get('branch', '?')} @ "
            f"{_short(base.get('sha'))} (tag {base.get('tag') or 'none'})"
        )
        return CLASS_INITIAL, reasons, errors

    if not diff.any_change:
        return CLASS_NO_CHANGE, ["no difference between base and head config"], []

    # ---- V6: base and topics must not move together ----
    if diff.touches_base and diff.touches_topics:
        errors.append(
            "V6: base fields and topic entries changed in the same PR "
            f"(base: {', '.join(sorted(diff.base_changes))}; "
            f"topics: {', '.join(sorted(set(diff.promoted) | set(diff.added) | set(diff.removed) | set(diff.redefined)))}). "
            "Split into an RC uprev PR and separate topic promotion PRs so a "
            "regression can be attributed to one of them."
        )
        return CLASS_INVALID, reasons, errors

    # ---- redefinition of repo/branch is never a promotion ----
    if diff.redefined:
        details = "; ".join(
            f"{name}: " + ", ".join(
                f"{k} {old!r} -> {new!r}" for k, (old, new) in fields.items()
            )
            for name, fields in diff.redefined.items()
        )
        errors.append(
            f"V6: topic repo/branch redefined ({details}). Changing where a topic "
            "comes from is not a SHA promotion; it must be reviewed as its own "
            "change with the old entry removed and the new one added."
        )
        return CLASS_INVALID, reasons, errors

    # ---- RC uprev ----
    if diff.touches_base:
        reasons.append(
            "base changed: "
            + ", ".join(
                f"{k} {old!r} -> {new!r}" for k, (old, new) in diff.base_changes.items()
            )
        )
        if diff.promoted:
            errors.append("V6: RC uprev must not change topic SHAs")
            return CLASS_INVALID, reasons, errors
        if diff.rerere_changes:
            reasons.append(
                "rerere cache also changed, which is allowed for an RC uprev when the "
                "new baseline conflicts with pinned topics"
            )
        return CLASS_RC_UPREV, reasons, errors

    # ---- topic add/remove ----
    if diff.added:
        reasons.append(f"topics added: {', '.join(diff.added)}")
    if diff.removed:
        reasons.append(f"topics removed: {', '.join(diff.removed)}")

    # ---- promotions ----
    if diff.promoted:
        for name, fields in diff.promoted.items():
            detail = ", ".join(
                f"{k} {_short(old)} -> {_short(new)}" for k, (old, new) in fields.items()
            )
            reasons.append(f"{name}: {detail}")

        promoted_names = list(diff.promoted)
        if len(promoted_names) == 1 and not diff.added and not diff.removed:
            return CLASS_TOPIC, reasons, errors

        justified, why = atomic_is_justified(promoted_names, diff.head)
        if justified:
            reasons.append(f"atomic promotion justified: {why}")
            return CLASS_ATOMIC, reasons, errors
        if allow_atomic_override:
            reasons.append(
                f"atomic promotion NOT justified by depends_on ({why}), but allowed "
                "by integration-owner override"
            )
            return CLASS_ATOMIC, reasons, errors
        errors.append(
            f"V6: multiple topic SHAs changed but {why}. Either declare the "
            "dependency with depends_on, split into sequential PRs, or re-run with "
            "integration-owner override."
        )
        return CLASS_INVALID, reasons, errors

    # ---- no promotions: rerere / metadata / state only ----
    if diff.added or diff.removed:
        # Adding or removing a topic changes what qcom-next contains, so treat it
        # as a promotion-class change requiring the full gates.
        return CLASS_ATOMIC if (len(diff.added) + len(diff.removed)) > 1 else CLASS_TOPIC, reasons, errors

    if diff.rerere_changes and not (diff.metadata or diff.order_changed):
        reasons.append(
            "rerere cache changed: "
            + ", ".join(
                f"{k} {_short(old)} -> {_short(new)}"
                for k, (old, new) in diff.rerere_changes.items()
            )
        )
        return CLASS_RERERE, reasons, errors

    if diff.metadata:
        for name, fields in diff.metadata.items():
            reasons.append(
                f"{name} metadata: "
                + ", ".join(f"{k} {old!r} -> {new!r}" for k, (old, new) in fields.items())
            )
    if diff.order_changed:
        reasons.append("topic merge order changed")
    if diff.promotion_state_change:
        old, new = diff.promotion_state_change
        reasons.append(f"promotion_state {old!r} -> {new!r}")
    if diff.schema_version_change:
        old, new = diff.schema_version_change
        reasons.append(f"schema_version {old!r} -> {new!r}")
    if diff.rerere_changes:
        reasons.append("rerere cache changed alongside metadata")

    # Order changes alter the generated tree, so they are not merely metadata.
    if diff.order_changed:
        return CLASS_ATOMIC, reasons, errors

    return CLASS_METADATA, reasons, errors


def _short(value) -> str:
    if isinstance(value, str) and len(value) == 40 and all(
        c in "0123456789abcdef" for c in value
    ):
        return value[:12]
    return repr(value)


# ---------------------------------------------------------------------------
# V7 — freeze enforcement
# ---------------------------------------------------------------------------
def check_freeze(diff: Diff, classification: str) -> Tuple[bool, List[str]]:
    """Return (publish_allowed, notes). Validation always proceeds."""
    notes: List[str] = []
    base_state = diff.base.get("promotion_state")
    head_state = diff.head.get("promotion_state")

    promotion_classes = {CLASS_TOPIC, CLASS_ATOMIC}

    if base_state == "frozen" and classification in promotion_classes:
        notes.append(
            "V7: promotion_state is frozen on the target branch (an RC uprev is in "
            "flight). This PR may run validation and CI, but must not publish or "
            "merge until the state returns to open (FR-RC-03)."
        )
        return False, notes

    if head_state == "frozen" and classification in promotion_classes:
        notes.append(
            "V7: this PR sets promotion_state to frozen while also promoting topics; "
            "freeze is set by the RC uprev flow, not by a promotion PR."
        )
        return False, notes

    if diff.promotion_state_change:
        old, new = diff.promotion_state_change
        if classification == CLASS_INITIAL:
            # There is no previous state to have changed; the field simply did
            # not exist before. Not worth reporting as a transition.
            pass
        elif classification == CLASS_RC_UPREV:
            notes.append(f"promotion_state {old!r} -> {new!r} as part of the RC uprev flow")
        else:
            notes.append(
                f"promotion_state {old!r} -> {new!r}; freeze/unfreeze is normally "
                "driven by the RC uprev workflow and needs integration-owner review"
            )
    return True, notes


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
GATE_PROFILE = {
    CLASS_RC_UPREV: "full (build, boot, functional, yocto, overlay, ubuntu)",
    CLASS_ATOMIC: "full (build, boot, functional, yocto, overlay, ubuntu)",
    CLASS_TOPIC: "full (build, boot, functional, yocto, overlay, ubuntu)",
    CLASS_INITIAL: "full (build, boot, functional, yocto, overlay, ubuntu)",
    CLASS_RERERE: "full - a rerere change alters conflict resolution, so the tree must be retested",
    CLASS_METADATA: "validation only - generated tree content is unchanged",
    CLASS_NO_CHANGE: "none",
    CLASS_INVALID: "none - PR rejected",
}


def render_markdown(classification: str, reasons: List[str], errors: List[str],
                    notes: List[str], publish_allowed: bool,
                    base_label: str, head_label: str) -> str:
    lines: List[str] = []
    a = lines.append
    verdict = "REJECTED" if errors else ("BLOCKED (freeze)" if not publish_allowed else "OK")
    a(f"### qcom-next change classification: `{classification}` — **{verdict}**")
    a("")
    a("| Field | Value |")
    a("|---|---|")
    a(f"| Classification | `{classification}` |")
    a(f"| Base | `{base_label}` |")
    a(f"| Head | `{head_label}` |")
    a(f"| Publication allowed | {'yes' if publish_allowed and not errors else 'no'} |")
    a(f"| Required gates | {GATE_PROFILE.get(classification, 'unknown')} |")
    a("")
    if reasons:
        a("**What changed**")
        a("")
        for r in reasons:
            a(f"- {r}")
        a("")
    if errors:
        a("**Blocking problems**")
        a("")
        for e in errors:
            a(f"- {e}")
        a("")
    if notes:
        a("**Notes**")
        a("")
        for n in notes:
            a(f"- {n}")
        a("")
    return "\n".join(lines)


def write_github_output(path: str, values: Dict[str, str]) -> None:
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for key, value in values.items():
                if "\n" in value:
                    fh.write(f"{key}<<__EOF__\n{value}\n__EOF__\n")
                else:
                    fh.write(f"{key}={value}\n")
    except OSError as exc:
        sys.stderr.write(f"warning: cannot write github output {path}: {exc}\n")


# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Classify a qcom-next.yaml change (rules V6 and V7).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit 0 = allowed, 1 = rejected or freeze-blocked, 2 = usage error.",
    )
    ap.add_argument("--config", default="qcom-next.yaml",
                    help="path of the config within the repo (default: %(default)s)")
    ap.add_argument("--base-ref", default=None,
                    help="git ref of the target branch, e.g. origin/main")
    ap.add_argument("--base-file", default=None,
                    help="base config as a file instead of a git ref (for testing)")
    ap.add_argument("--head-file", default=None,
                    help="head config file (default: --config in the working tree)")
    ap.add_argument("--repo-dir", default=".",
                    help="repository directory for git operations (default: %(default)s)")
    ap.add_argument("--allow-atomic-override", action="store_true",
                    help="permit a multi-topic promotion that depends_on does not "
                         "justify (integration-owner override)")
    ap.add_argument("--format", choices=["text", "json", "markdown"], default="text",
                    help="output format (default: %(default)s)")
    ap.add_argument("--markdown-out", metavar="PATH",
                    help="also write a Markdown summary to PATH")
    ap.add_argument("--github-output", metavar="PATH", default=None,
                    help="append key=value outputs for GitHub Actions "
                         "(defaults to $GITHUB_OUTPUT when set)")
    ap.add_argument("--ignore-freeze", action="store_true",
                    help="report the freeze state but do not fail on it")
    args = ap.parse_args(argv)

    if not args.base_ref and not args.base_file:
        sys.stderr.write("error: one of --base-ref or --base-file is required\n")
        return 2

    base_absent = False
    try:
        if args.base_file:
            base_cfg = load_config_file(args.base_file)
            base_label = args.base_file
        else:
            try:
                base_cfg = load_config_from_git(args.base_ref, args.config, args.repo_dir)
                base_label = f"{args.base_ref}:{args.config}"
            except BaseConfigAbsent as exc:
                # First introduction of the config, or a manual run against a
                # branch that predates it. Not an error.
                sys.stderr.write(f"note: {exc}; treating this as an initial import\n")
                base_cfg = {}
                base_absent = True
                base_label = f"{args.base_ref}:{args.config} (absent)"

        head_path = args.head_file or os.path.join(args.repo_dir, args.config)
        head_cfg = load_config_file(head_path)
        head_label = head_path
    except ClassifyError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    diff = Diff(base_cfg, head_cfg)
    classification, reasons, errors = classify(
        diff, args.allow_atomic_override, base_absent=base_absent
    )
    publish_allowed, notes = check_freeze(diff, classification)

    blocked = bool(errors) or (not publish_allowed and not args.ignore_freeze)

    if args.format == "json":
        print(json.dumps({
            "classification": classification,
            "publish_allowed": publish_allowed and not errors,
            "required_gates": GATE_PROFILE.get(classification),
            "reasons": reasons,
            "errors": errors,
            "notes": notes,
            "base": base_label,
            "head": head_label,
            "promoted_topics": sorted(diff.promoted),
            "added_topics": diff.added,
            "removed_topics": diff.removed,
            "base_fields_changed": sorted(diff.base_changes),
            "rerere_changed": sorted(diff.rerere_changes),
        }, indent=2))
    elif args.format == "markdown":
        print(render_markdown(classification, reasons, errors, notes,
                              publish_allowed, base_label, head_label))
    else:
        print(f"qcom-next change classification: {classification}")
        print(f"  base            : {base_label}")
        print(f"  head            : {head_label}")
        print(f"  publish allowed : {'yes' if publish_allowed and not errors else 'no'}")
        print(f"  required gates  : {GATE_PROFILE.get(classification, 'unknown')}")
        if reasons:
            print("\nWhat changed:")
            for r in reasons:
                print(f"  - {r}")
        if notes:
            print("\nNotes:")
            for n in notes:
                print(f"  - {n}")
        if errors:
            print("\nBlocking problems:")
            for e in errors:
                print(f"  - {e}")
        print()
        print("RESULT: REJECTED" if errors else
              ("RESULT: BLOCKED (promotion freeze)" if blocked else "RESULT: OK"))

    if args.markdown_out:
        try:
            with open(args.markdown_out, "w", encoding="utf-8") as fh:
                fh.write(render_markdown(classification, reasons, errors, notes,
                                         publish_allowed, base_label, head_label) + "\n")
        except OSError as exc:
            sys.stderr.write(f"warning: cannot write {args.markdown_out}: {exc}\n")

    out_path = args.github_output or os.environ.get("GITHUB_OUTPUT")
    if out_path:
        write_github_output(out_path, {
            "classification": classification,
            "publish_allowed": "true" if (publish_allowed and not errors) else "false",
            "promoted_topics": ",".join(sorted(diff.promoted)),
            "required_gates": GATE_PROFILE.get(classification, ""),
            "blocked": "true" if blocked else "false",
        })

    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
