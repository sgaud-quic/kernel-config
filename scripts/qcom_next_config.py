#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Helper that projects qcom-next.yaml into the formats the shell scripts need.

Keeping YAML handling in one place avoids a `yq` dependency in CI and keeps
generate_qcom_next_tree.sh readable. Every subcommand writes plain,
tab-separated or line-oriented text that is trivial to consume from shell.

Subcommands:

  base          TSV: repo, branch, tag, sha
  rerere        TSV: repo, ref, sha
  topics        TSV per topic: name, repo, branch, sha, tag
  merge-conf    the flat three-column conf consumed by automerge's ci-merge
  pin-refs      TSV: refname, sha  - local refs to create before merging
  ref-clashes   report git directory/file ref-name collisions
  tested-set    canonical record of what a generated tree must contain
  digest        stable digest of a tested-set file

The tested-set format is the contract between generation (Phase 2) and
publication (Phase 5): publication regenerates the tree and refuses to publish
unless the digest matches what CI tested.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from typing import Dict, List, Optional, Tuple

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TESTED_SET_VERSION = 1


def load(path: str) -> dict:
    try:
        import yaml
    except ImportError:
        sys.stderr.write("error: PyYAML is required (pip install pyyaml)\n")
        raise SystemExit(2)
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        sys.stderr.write(f"error: config not found: {path}\n")
        raise SystemExit(2)
    except yaml.YAMLError as exc:
        sys.stderr.write(f"error: {path} is not valid YAML: {exc}\n")
        raise SystemExit(2)
    if not isinstance(data, dict):
        sys.stderr.write(f"error: {path}: top level must be a mapping\n")
        raise SystemExit(2)
    return data


def topics_of(cfg: dict) -> List[dict]:
    raw = cfg.get("topics")
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, dict) and isinstance(t.get("name"), str)]


def _require_sha(value, what: str) -> str:
    if not isinstance(value, str) or not SHA_RE.match(value):
        sys.stderr.write(
            f"error: {what} is not a full 40-hex lowercase sha: {value!r}\n"
            "Tree generation requires every pin to be concrete; run the validator.\n"
        )
        raise SystemExit(2)
    return value


EMPTY = "-"


def _field(value) -> str:
    """Render a field for tab-separated shell consumption.

    Empty fields are written as "-" rather than left blank. Tab is an IFS
    whitespace character, so bash collapses consecutive tabs and a blank field
    would silently shift every field after it - which would make a null tag look
    like a sha. The sentinel keeps the column count fixed.
    """
    if value is None:
        return EMPTY
    text = str(value)
    return text if text != "" else EMPTY


# ---------------------------------------------------------------------------
def cmd_base(cfg: dict, args) -> int:
    base = cfg.get("base") or {}
    print("\t".join([
        _field(base.get("repo")),
        _field(base.get("branch")),
        _field(base.get("tag")),
        _field(base.get("sha")),
    ]))
    return 0


def cmd_rerere(cfg: dict, args) -> int:
    rr = cfg.get("rerere_cache") or {}
    print("\t".join([
        _field(rr.get("repo")),
        _field(rr.get("ref")),
        _field(rr.get("sha")),
    ]))
    return 0


def cmd_topics(cfg: dict, args) -> int:
    for t in topics_of(cfg):
        print("\t".join([
            _field(t["name"]),
            _field(t.get("repo")),
            _field(t.get("branch")),
            _field(t.get("sha")),
            _field(t.get("tag")),
        ]))
    return 0


def cmd_merge_conf(cfg: dict, args) -> int:
    """Emit the flat conf ci-merge reads: '<name> <url> <branch>'.

    With --url-override every row points at one local repository. That is what
    makes the generated tree deterministic: ci-merge derives the merge base from
    remotes/<baseline>/<branch>, so the only way to pin base.sha is for the
    baseline remote to be a repo whose branch sits at that commit. Pointing all
    rows at the local worktree also turns 78 network fetches into local ones.
    """
    base = cfg.get("base") or {}
    base_branch = base.get("branch")
    if not isinstance(base_branch, str) or not base_branch:
        sys.stderr.write("error: base.branch is required\n")
        return 2

    url_for = (lambda _original: args.url_override) if args.url_override else (lambda o: o)

    rows: List[Tuple[str, str, str]] = [
        (args.baseline_name, url_for(str(base.get("repo", ""))), base_branch)
    ]
    for t in topics_of(cfg):
        branch = t.get("branch")
        if not isinstance(branch, str) or not branch:
            sys.stderr.write(f"error: topic {t['name']!r} has no branch\n")
            return 2
        if t["name"] == args.baseline_name:
            sys.stderr.write(
                f"error: topic {t['name']!r} collides with the baseline row name; "
                "ci-merge identifies the baseline by name and would skip this topic\n"
            )
            return 2
        rows.append((t["name"], url_for(str(t.get("repo", ""))), branch))

    width_name = max(len(r[0]) for r in rows)
    width_url = max(len(r[1]) for r in rows)

    out: List[str] = []
    if not args.no_header:
        out.append("# Generated from the SHA-pinned qcom-next configuration.")
        out.append("# DO NOT EDIT and DO NOT COMMIT: this is a disposable CI artifact.")
        out.append("# The reviewed source of truth is qcom-next.yaml.")
        if args.url_override:
            out.append("#")
            out.append("# All rows intentionally point at one local repository whose branches")
            out.append("# have been reset to the pinned SHAs, so ci-merge consumes the pins")
            out.append("# rather than live branch tips.")
        out.append("")
    for name, url, branch in rows:
        out.append(f"{name:<{width_name}}  {url:<{width_url}}  {branch}")

    text = "\n".join(out) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        sys.stderr.write(f"wrote {args.output} ({len(rows)} rows)\n")
    else:
        sys.stdout.write(text)
    return 0


def cmd_pin_refs(cfg: dict, args) -> int:
    """Local refs to create so ci-merge merges pinned commits.

    Two kinds are emitted:

      refs/heads/<branch>        the branch ci-merge fetches through the
                                 file:// remote; pins the baseline and gives
                                 each topic remote a resolvable branch
      refs/heads/override/<name> checked first by ci-merge (ci-merge:645-650),
                                 so it is the authoritative topic pin
    """
    base = cfg.get("base") or {}
    base_branch = base.get("branch")
    base_sha = _require_sha(base.get("sha"), "base.sha")
    rows: List[Tuple[str, str]] = []
    if isinstance(base_branch, str) and base_branch:
        rows.append((f"refs/heads/{base_branch}", base_sha))

    for t in topics_of(cfg):
        sha = _require_sha(t.get("sha"), f"topics[{t['name']}].sha")
        branch = t.get("branch")
        if isinstance(branch, str) and branch:
            rows.append((f"refs/heads/{branch}", sha))
        rows.append((f"refs/heads/override/{t['name']}", sha))

    for ref, sha in rows:
        print(f"{ref}\t{sha}")
    return 0


def _clashes(refs: List[str]) -> List[Tuple[str, str]]:
    """git cannot store refs/a and refs/a/b simultaneously (D/F conflict)."""
    found: List[Tuple[str, str]] = []
    ordered = sorted(set(refs))
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if b.startswith(a + "/"):
                found.append((a, b))
    return found


def cmd_ref_clashes(cfg: dict, args) -> int:
    base = cfg.get("base") or {}
    refs: List[str] = []
    if isinstance(base.get("branch"), str):
        refs.append(f"refs/heads/{base['branch']}")
    for t in topics_of(cfg):
        if isinstance(t.get("branch"), str):
            refs.append(f"refs/heads/{t['branch']}")
        refs.append(f"refs/heads/override/{t['name']}")

    bad = _clashes(refs)
    for a, b in bad:
        print(f"CLASH\t{a}\t{b}")
    if bad:
        sys.stderr.write(
            f"error: {len(bad)} ref name collision(s); git cannot hold a ref and a "
            "directory of refs at the same path\n"
        )
        return 1
    sys.stderr.write(f"no ref name collisions among {len(set(refs))} refs\n")
    return 0


def _tested_set_lines(cfg: dict, generated_sha: Optional[str]) -> List[str]:
    base = cfg.get("base") or {}
    rr = cfg.get("rerere_cache") or {}
    lines: List[str] = [
        f"version\t{TESTED_SET_VERSION}",
        f"base\t{_require_sha(base.get('sha'), 'base.sha')}",
        f"base_branch\t{base.get('branch', '')}",
        f"base_tag\t{'' if base.get('tag') is None else base['tag']}",
        f"rerere_repo\t{rr.get('repo', '')}",
        f"rerere_ref\t{rr.get('ref', '')}",
        f"rerere_sha\t{'' if rr.get('sha') is None else rr['sha']}",
    ]
    # Sorted by name so the digest is independent of file ordering; merge order
    # is captured separately because it affects the resulting tree.
    for t in sorted(topics_of(cfg), key=lambda x: x["name"]):
        lines.append(f"topic\t{t['name']}\t{_require_sha(t.get('sha'), t['name'] + '.sha')}")
    order = [t["name"] for t in topics_of(cfg)]
    lines.append("order\t" + ",".join(order))
    if generated_sha:
        lines.append(f"generated_head\t{generated_sha}")
    return lines


def _digest(lines: List[str]) -> str:
    """Digest of the inputs only; generated_head is excluded deliberately.

    Two runs from the same inputs must agree even though ci-merge writes merge
    commits with differing committer timestamps, so the integration HEAD is not
    reproducible byte-for-byte while the consumed content is.
    """
    payload = "\n".join(
        ln for ln in lines if not ln.startswith(("generated_head\t", "digest\t"))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cmd_tested_set(cfg: dict, args) -> int:
    lines = _tested_set_lines(cfg, args.generated_head)
    digest = _digest(lines)
    out = ["# qcom-next tested set - inputs that define the generated tree",
           "# Consumed by check_tested_tree_matches.sh before publication.",
           f"digest\t{digest}"] + lines
    text = "\n".join(out) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        sys.stderr.write(f"wrote {args.output} (digest {digest[:16]})\n")
    else:
        sys.stdout.write(text)
    return 0


def cmd_digest(cfg: dict, args) -> int:
    """Recompute the digest of an existing tested-set file.

    Reading the file back rather than trusting its digest line is what makes
    tampering or truncation detectable.
    """
    try:
        with open(args.tested_set, encoding="utf-8") as fh:
            raw = [ln.rstrip("\n") for ln in fh]
    except OSError as exc:
        sys.stderr.write(f"error: cannot read {args.tested_set}: {exc}\n")
        return 2
    lines = [ln for ln in raw if ln and not ln.startswith("#")]
    recorded = next((ln.split("\t", 1)[1] for ln in lines if ln.startswith("digest\t")), None)
    computed = _digest(lines)
    if args.quiet:
        print(computed)
    else:
        print(f"recorded\t{recorded or '<none>'}")
        print(f"computed\t{computed}")
    if recorded is None:
        # A missing digest line is not "nothing to check": this command is the
        # tamper check that check_tested_tree_matches.sh runs before publishing,
        # and truncating the file or deleting the digest line used to pass it.
        sys.stderr.write(
            f"error: {args.tested_set} has no digest line, so it cannot be "
            "verified; it was truncated or is not a tested-set file\n")
        return 1
    if recorded != computed:
        sys.stderr.write("error: tested-set digest does not match its contents\n")
        return 1
    return 0


# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="qcom-next.yaml", help="config (default: %(default)s)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("base", help="TSV: repo, branch, tag, sha")
    sub.add_parser("rerere", help="TSV: repo, ref, sha")
    sub.add_parser("topics", help="TSV per topic: name, repo, branch, sha, tag")
    sub.add_parser("ref-clashes", help="report git D/F ref name collisions")
    sub.add_parser("pin-refs", help="TSV: refname, sha")

    mc = sub.add_parser("merge-conf", help="emit the flat ci-merge conf")
    mc.add_argument("--url-override", default=None,
                    help="use this URL for every row (e.g. file:///path/to/kernel)")
    mc.add_argument("--baseline-name", default="baseline",
                    help="name of the baseline row (default: %(default)s)")
    mc.add_argument("--output", default=None, help="write here instead of stdout")
    mc.add_argument("--no-header", action="store_true", help="omit the comment header")

    ts = sub.add_parser("tested-set", help="emit the canonical tested-set record")
    ts.add_argument("--generated-head", default=None,
                    help="integration branch HEAD, recorded but excluded from the digest")
    ts.add_argument("--output", default=None, help="write here instead of stdout")

    dg = sub.add_parser("digest", help="recompute the digest of a tested-set file")
    dg.add_argument("tested_set", help="tested-set file")
    dg.add_argument("--quiet", action="store_true", help="print only the computed digest")

    args = ap.parse_args(argv)

    # `digest` validates an existing artifact and must not require the config.
    cfg = {} if args.cmd == "digest" else load(args.config)

    table = {
        "base": cmd_base,
        "rerere": cmd_rerere,
        "topics": cmd_topics,
        "merge-conf": cmd_merge_conf,
        "pin-refs": cmd_pin_refs,
        "ref-clashes": cmd_ref_clashes,
        "tested-set": cmd_tested_set,
        "digest": cmd_digest,
    }
    return table[args.cmd](cfg, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Normal when output is piped into head/grep -q. Avoid the interpreter's
        # "Exception ignored" noise on shutdown.
        try:
            sys.stdout.close()
        except BrokenPipeError:
            pass
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
