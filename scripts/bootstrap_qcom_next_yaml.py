#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Bootstrap qcom-next.yaml from the legacy flat qcom-next.conf.

This is a one-shot migration helper for Phase 2 of the kernel topic promotion
flow. It reads the legacy three-column conf consumed by automerge's ci-merge::

    <name> <url> <branch>

resolves the current tip SHA of every branch from its remote, discovers any
existing tag that already points at that tip, and emits the SHA-pinned
``qcom-next.yaml`` that becomes the single reviewed source of truth.

After the initial migration, ``qcom-next.yaml`` is edited by maintainers through
pull requests; this script is not part of the normal promotion flow. It is kept
in-tree so the migration is reproducible and auditable.

Usage::

    scripts/bootstrap_qcom_next_yaml.py \\
        --conf qcom-next.conf \\
        --output qcom-next.yaml \\
        --base-tag v7.3-rc1

Network access to every configured remote is required unless --dry-run is used.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import subprocess
import sys
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
COMMENT_RE = re.compile(r"(^#|^\s*$|^\s*\t*#)")

DEFAULT_RERERE_REPO = "https://github.com/sgaud-quic/cache"
DEFAULT_RERERE_REF = "master"
SCHEMA_VERSION = 1


class BootstrapError(RuntimeError):
    """Fatal bootstrap failure."""


# ---------------------------------------------------------------------------
# legacy conf parsing
# ---------------------------------------------------------------------------
def parse_conf(path: str, baseline_name: str) -> Tuple[dict, List[dict]]:
    """Parse the legacy conf into (baseline_row, topic_rows).

    ci-merge identifies the baseline by *name*, not by position, so we do the
    same rather than assuming it is the first row.
    """
    baseline: Optional[dict] = None
    topics: List[dict] = []
    seen: Dict[str, int] = {}

    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            if COMMENT_RE.match(raw):
                continue
            fields = raw.split()
            if len(fields) < 3:
                raise BootstrapError(
                    f"{path}:{lineno}: expected '<name> <url> <branch>', got: {raw.strip()!r}"
                )
            name, url, branch = fields[0], fields[1], fields[2]

            if name == baseline_name:
                if baseline is not None:
                    raise BootstrapError(
                        f"{path}:{lineno}: duplicate baseline row for {baseline_name!r}"
                    )
                baseline = {"name": name, "repo": url, "branch": branch, "lineno": lineno}
                continue

            if name in seen:
                raise BootstrapError(
                    f"{path}:{lineno}: duplicate topic {name!r} (first seen at line {seen[name]})"
                )
            seen[name] = lineno
            topics.append({"name": name, "repo": url, "branch": branch, "lineno": lineno})

    if baseline is None:
        raise BootstrapError(
            f"{path}: no baseline row named {baseline_name!r}; ci-merge would abort"
        )
    if not topics:
        raise BootstrapError(f"{path}: no topic rows found")
    return baseline, topics


# ---------------------------------------------------------------------------
# remote resolution
# ---------------------------------------------------------------------------
def _ls_remote(repo: str, *extra: str) -> List[Tuple[str, str]]:
    cmd = ["git", "ls-remote", *extra, repo]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(f"git ls-remote timed out for {repo}") from exc
    if proc.returncode != 0:
        raise BootstrapError(
            f"git ls-remote failed for {repo} (exit {proc.returncode}): "
            f"{proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else 'no stderr'}"
        )
    out: List[Tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            out.append((parts[0].strip(), parts[1].strip()))
    return out


def load_remote_maps(repos: List[str], want_tags: bool) -> Dict[str, dict]:
    """For each repo return {'heads': {branch: sha}, 'tag_by_sha': {sha: [tags]}}."""
    maps: Dict[str, dict] = {}
    for repo in repos:
        sys.stderr.write(f"  resolving heads: {repo}\n")
        heads: Dict[str, str] = {}
        for sha, ref in _ls_remote(repo, "--heads"):
            if ref.startswith("refs/heads/"):
                heads[ref[len("refs/heads/"):]] = sha

        tag_by_sha: Dict[str, List[str]] = {}
        if want_tags:
            sys.stderr.write(f"  resolving tags : {repo}\n")
            raw: Dict[str, str] = {}
            for sha, ref in _ls_remote(repo, "--tags"):
                if not ref.startswith("refs/tags/"):
                    continue
                raw[ref[len("refs/tags/"):]] = sha
            # Peeled refs (tag^{}) hold the commit an annotated tag points to and
            # take precedence over the tag object's own SHA.
            for tag, sha in raw.items():
                if tag.endswith("^{}"):
                    tag_by_sha.setdefault(sha, []).append(tag[:-3])
            for tag, sha in raw.items():
                if tag.endswith("^{}"):
                    continue
                if f"{tag}^{{}}" in raw:
                    continue  # already recorded via its peeled form
                tag_by_sha.setdefault(sha, []).append(tag)

        maps[repo] = {"heads": heads, "tag_by_sha": tag_by_sha}
    return maps


def pick_tag(candidates: List[str], branch: str) -> Optional[str]:
    """Return this topic's own tag for the pinned commit, else None.

    Only tags following the project convention ``<branch>-<kver>-<YYYYMMDD>`` are
    accepted. Several topics frequently share a commit (a common merge base), so
    a tag pointing at the right SHA may well belong to a *different* topic.
    Adopting it would record a misleading cross-topic association that breaks as
    soon as the other topic is retagged, so we emit None instead and let the
    maintainer create a proper tag.
    """
    own = [t for t in candidates if t.startswith(f"{branch}-")]
    if not own:
        return None
    return sorted(own)[-1]


# ---------------------------------------------------------------------------
# YAML emission (hand-rendered for comments + stable minimal diffs)
# ---------------------------------------------------------------------------
def _scalar(value) -> str:
    if value is None:
        return "null"
    text = str(value)
    if text == "":
        return '""'
    # Quote anything that could be misread as a non-string YAML scalar.
    if re.search(r"[:#{}\[\],&*?|<>=!%@`'\"]", text) or text[0] in "-? " or text[-1] == " ":
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if text.lower() in {"true", "false", "null", "yes", "no", "on", "off", "~"}:
        return f'"{text}"'
    if re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", text):
        return f'"{text}"'
    return text


def render_yaml(base: dict, topics: List[dict], promotion_state: str,
                rerere_repo: str, rerere_ref: str, rerere_sha: Optional[str],
                source_conf: str, untagged: List[str],
                dropped: Optional[List[dict]] = None) -> str:
    today = _dt.date.today().isoformat()
    out: List[str] = []
    a = out.append

    a("# qcom-next SHA-pinned topic configuration")
    a("#")
    a("# SINGLE SOURCE OF TRUTH for the content of the qcom-next integration tree.")
    a("#")
    a("#   qcom-next = f( base.sha , ordered topics[].sha , rerere cache revision )")
    a("#")
    a("# Rules:")
    a("#   * qcom-next is never hand-edited. It is published only by the")
    a("#     publication workflow, driven by this file.")
    a("#   * Every sha recorded here is a STABLE sha: it has passed the CI gates.")
    a("#   * Every sha must be reachable from a tag so it survives a topic rebase.")
    a("#   * Topic owners live in topic_maintainers.json, keyed by the topic name")
    a("#     below. Do not duplicate owner data here.")
    a("#   * Array order IS merge order and must respect depends_on.")
    a("#")
    a("# To promote a topic: open a PR changing only that topic's sha and tag.")
    a("# To uprev the kernel RC: open a PR changing only the base block.")
    a("# Validated by scripts/validate_qcom_next_config.py against")
    a("# schemas/qcom-next.schema.json.")
    a("#")
    a(f"# Bootstrapped from {source_conf} on {today} by")
    a("# scripts/bootstrap_qcom_next_yaml.py (branch tips captured as the initial")
    a("# pinned SHAs).")
    if dropped:
        a("#")
        a(f"# REMOVED during bootstrap: {len(dropped)} topic(s) listed in the legacy conf")
        a("# no longer exist on their remote and were dropped (FR-INT-02). Restore the")
        a("# branch and re-add the entry if this was not intended:")
        for d in dropped:
            a(f"#   - {d['name']}  (was {source_conf}:{d['lineno']}, branch {d['branch']})")
    if untagged:
        a("#")
        a(f"# NOTE: {len(untagged)} topic(s) had no tag pointing at the captured tip at")
        a("# bootstrap time and carry 'tag: null'. Each must be tagged")
        a("# (<branch>-<kernel-version>-<YYYYMMDD>) before it can be promoted; the")
        a("# validator rejects a null tag unless --allow-untagged is passed.")
    a("")
    a(f"schema_version: {SCHEMA_VERSION}")
    a("")
    a("# Upstream baseline. Changed only by an RC uprev PR (Phase A), which must")
    a("# leave every topic sha untouched.")
    a("base:")
    a(f"  repo: {_scalar(base['repo'])}")
    a(f"  branch: {_scalar(base['branch'])}")
    a(f"  tag: {_scalar(base.get('tag'))}")
    a(f"  sha: {_scalar(base.get('sha'))}")
    a("")
    a("# open   = topic promotions may publish")
    a("# frozen = RC uprev in progress; promotions may validate but not publish")
    a(f"promotion_state: {_scalar(promotion_state)}")
    a("")
    a("# Shared rerere cache replayed during tree generation. Read-only in normal")
    a("# PR CI; updated by a topic maintainer only when resolving a conflict.")
    a("rerere_cache:")
    a(f"  repo: {_scalar(rerere_repo)}")
    a(f"  ref: {_scalar(rerere_ref)}")
    if rerere_sha:
        a(f"  sha: {_scalar(rerere_sha)}")
    a("")
    a(f"# {len(topics)} topic branches, in merge order.")
    a("topics:")
    for t in topics:
        a(f"  - name: {_scalar(t['name'])}")
        a(f"    repo: {_scalar(t['repo'])}")
        a(f"    branch: {_scalar(t['branch'])}")
        a(f"    sha: {_scalar(t.get('sha'))}")
        a(f"    tag: {_scalar(t.get('tag'))}")
        deps = t.get("depends_on") or []
        if deps:
            a("    depends_on:")
            for d in deps:
                a(f"      - {_scalar(d)}")
        else:
            a("    depends_on: []")
    a("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Bootstrap qcom-next.yaml from the legacy qcom-next.conf.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--conf", default="qcom-next.conf",
                    help="legacy flat conf to migrate (default: %(default)s)")
    ap.add_argument("--output", default="qcom-next.yaml",
                    help="YAML file to write (default: %(default)s)")
    ap.add_argument("--baseline-name", default="baseline",
                    help="row name ci-merge treats as the baseline (default: %(default)s)")
    ap.add_argument("--base-tag", default=None,
                    help="upstream tag/RC label for the baseline, e.g. v7.3-rc1")
    ap.add_argument("--promotion-state", default="open", choices=["open", "frozen"],
                    help="initial promotion state (default: %(default)s)")
    ap.add_argument("--rerere-repo", default=DEFAULT_RERERE_REPO,
                    help="rerere cache repo (default: %(default)s)")
    ap.add_argument("--rerere-ref", default=DEFAULT_RERERE_REF,
                    help="rerere cache ref (default: %(default)s)")
    ap.add_argument("--pin-rerere-sha", action="store_true",
                    help="also resolve and pin the rerere cache commit")
    ap.add_argument("--no-tags", action="store_true",
                    help="skip tag discovery; emit tag: null for every topic")
    ap.add_argument("--skip-missing", action="store_true",
                    help="drop topics whose branch no longer exists on its remote "
                         "instead of failing. Per FR-INT-02 a deleted branch must be "
                         "removed from the config; this records the removal explicitly")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse the conf and report, without contacting remotes")
    args = ap.parse_args(argv)

    try:
        baseline, topics = parse_conf(args.conf, args.baseline_name)
    except (OSError, BootstrapError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    sys.stderr.write(
        f"parsed {args.conf}: baseline={baseline['name']} "
        f"({baseline['branch']}), {len(topics)} topics\n"
    )

    if args.dry_run:
        for t in topics:
            print(f"{t['name']}\t{t['branch']}\t{t['repo']}")
        return 0

    repos = list(OrderedDict.fromkeys([baseline["repo"]] + [t["repo"] for t in topics]))
    sys.stderr.write(f"resolving {len(repos)} remote(s)\n")
    try:
        maps = load_remote_maps(repos, want_tags=not args.no_tags)
    except BootstrapError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    # baseline
    bheads = maps[baseline["repo"]]["heads"]
    if baseline["branch"] not in bheads:
        sys.stderr.write(
            f"error: baseline branch {baseline['branch']!r} not found in {baseline['repo']}\n"
        )
        return 2
    base_out = {
        "repo": baseline["repo"],
        "branch": baseline["branch"],
        "tag": args.base_tag,
        "sha": bheads[baseline["branch"]],
    }

    # topics
    missing: List[dict] = []
    untagged: List[str] = []
    for t in topics:
        heads = maps[t["repo"]]["heads"]
        if t["branch"] not in heads:
            missing.append(t)
            t["sha"] = None
            t["tag"] = None
            continue
        sha = heads[t["branch"]]
        t["sha"] = sha
        tag = None if args.no_tags else pick_tag(
            maps[t["repo"]]["tag_by_sha"].get(sha, []), t["branch"]
        )
        t["tag"] = tag
        if tag is None:
            untagged.append(t["name"])

    if missing:
        sys.stderr.write("\ntopic branches not found on their remotes:\n")
        for m in missing:
            sys.stderr.write(
                f"  - {m['name']}  (branch {m['branch']!r} absent from {m['repo']},"
                f" {args.conf}:{m['lineno']})\n"
            )
        if not args.skip_missing:
            sys.stderr.write(
                "\nerror: refusing to write a config referencing missing branches.\n"
                "Either restore the branch, correct the rename in the conf, or re-run\n"
                "with --skip-missing to drop the entries (FR-INT-02: a deleted branch\n"
                "must also be removed from the config).\n"
            )
            return 2
        dropped = {m["name"] for m in missing}
        topics = [t for t in topics if t["name"] not in dropped]
        sys.stderr.write(
            f"\n--skip-missing: dropped {len(dropped)} topic(s) from the generated config.\n"
            "Review this removal; it changes what qcom-next will contain.\n"
        )
        if not topics:
            sys.stderr.write("error: no topics remain after dropping missing branches\n")
            return 2

    rerere_sha = None
    if args.pin_rerere_sha:
        try:
            for sha, ref in _ls_remote(args.rerere_repo, "--heads"):
                if ref == f"refs/heads/{args.rerere_ref}":
                    rerere_sha = sha
                    break
        except BootstrapError as exc:
            sys.stderr.write(f"warning: could not pin rerere sha: {exc}\n")
        if rerere_sha is None:
            sys.stderr.write(
                f"warning: ref {args.rerere_ref!r} not found in {args.rerere_repo}\n"
            )

    text = render_yaml(
        base_out, topics, args.promotion_state,
        args.rerere_repo, args.rerere_ref, rerere_sha,
        args.conf, untagged, missing if args.skip_missing else None,
    )
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(text)

    tagged = len(topics) - len(untagged)
    sys.stderr.write(
        f"\nwrote {args.output}\n"
        f"  base      : {base_out['branch']} @ {base_out['sha'][:12]}"
        f" (tag: {base_out['tag'] or 'null'})\n"
        f"  topics    : {len(topics)}\n"
        f"  tagged    : {tagged}\n"
        f"  untagged  : {len(untagged)}\n"
    )
    if untagged:
        sys.stderr.write(
            "  topics needing a tag before promotion:\n"
        )
        for name in untagged:
            sys.stderr.write(f"    - {name}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
