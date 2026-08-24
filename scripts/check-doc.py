#!/usr/bin/env python3
"""Verify the guide against itself.

This document argues that a guardrail nobody has watched succeed may never have
run, and that receipts beat opinions. It then shipped a broken anchor link,
because the anchor was checked with a hand-rolled slug function that was wrong
and had never been compared against what GitHub actually emits.

So the first thing this script does is prove its own slug algorithm reproduces
real GitHub output. Everything after that is downstream of that check passing.

    scripts/check-doc.py            # structural checks
    scripts/check-doc.py --links    # also verify external URLs (network)

Exit 0 clean, 1 on any failure.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "README.md"

_passed = 0
_failed = 0


def ok(msg: str) -> None:
    global _passed
    _passed += 1
    print(f"  ok   {msg}")


def bad(msg: str, detail: str = "") -> None:
    global _failed
    _failed += 1
    print(f"  FAIL {msg}")
    for line in filter(None, detail.splitlines()):
        print(f"       {line}")


def slug(heading: str) -> str:
    """GitHub's heading-anchor algorithm.

    Lowercase; drop every character that is not alphanumeric, space, hyphen or
    underscore; then map each space to one hyphen.

    The last clause is the one that bites: runs of spaces are NOT collapsed. An
    em dash is *removed*, leaving the spaces on both sides of it, so `Stop — gate`
    becomes `stop--gate` with two hyphens. Collapsing them is what broke the
    §6.3 link, and it is why check_slug_algorithm() below exists.
    """
    s = heading.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    return s.replace(" ", "-")


# Ground truth: anchor IDs GitHub actually generated for this document, taken
# from the rendered page (id="user-content-..."). If GitHub changes its
# algorithm, this fails here rather than silently passing a broken link.
GITHUB_ANCHORS = {
    "## 8. `.claude/` is an attack surface": "8-claude-is-an-attack-surface",
    "### 2.5 Four things that catch people": "25-four-things-that-catch-people",
    "### 6.4 Notifications": "64-notifications",
    "### 6.1 `SessionStart` — inject the context that must always be present": "61-sessionstart--inject-the-context-that-must-always-be-present",
    '### 6.3 `Stop` — gate "done" on evidence, not on the model\'s opinion': "63-stop--gate-done-on-evidence-not-on-the-models-opinion",
}


def check_slug_algorithm() -> None:
    print("slug algorithm — validated against real GitHub output")
    for heading, expected in GITHUB_ANCHORS.items():
        text = heading.lstrip("#").strip()
        got = slug(text)
        if got == expected:
            ok(f"{expected[:52]}")
        else:
            bad(
                f"slug mismatch for {heading[:44]!r}",
                f"expected: {expected}\nactual:   {got}",
            )


def check_anchors(doc: str) -> None:
    print("\ninternal links")
    heads = {slug(m.group(1)) for m in re.finditer(r"^#{2,4}\s+(.+)$", doc, re.M)}
    links = re.findall(r"\]\(#([^)]+)\)", doc)
    broken = sorted({l for l in links if l not in heads})
    if broken:
        bad(
            f"{len(broken)} broken anchor(s) of {len(links)}",
            "\n".join(f"#{b}" for b in broken),
        )
    else:
        ok(f"all {len(links)} anchors resolve to real headings")


def check_fences(doc: str) -> None:
    print("\nstructure")
    n = sum(1 for line in doc.splitlines() if line.startswith("```"))
    ok(f"{n} code fences, balanced") if n % 2 == 0 else bad(
        "unbalanced code fences", f"{n} found"
    )


def check_bash_blocks(doc: str) -> None:
    print("\nbash blocks parse")
    blocks = re.findall(r"```bash\n(.*?)```", doc, re.S)
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, block in enumerate(blocks):
            # Fragments (case arms, loose snippets) aren't standalone scripts.
            if not re.search(
                r"^(#!|set |[a-z_]+\(\) \{|if |require |PAYLOAD=)", block, re.M
            ):
                continue
            p = Path(tmp) / f"b{i}.sh"
            p.write_text(block)
            r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
            if r.returncode != 0:
                failures.append(
                    f"block {i}: {r.stderr.strip().splitlines()[0] if r.stderr else '?'}"
                )
    if failures:
        bad(f"{len(failures)} bash block(s) fail syntax check", "\n".join(failures))
    else:
        ok(f"{len(blocks)} bash blocks, all syntax-clean")


def check_preamble_behaviour(doc: str) -> None:
    """Extract the published preamble and assert its helpers actually work.

    Two of this document's shipped bugs -- `jq -er` conflating absent with
    false, and `require` reading only $1 -- were invisible to every reading and
    obvious the moment anyone ran them.
    """
    print("\npublished preamble behaves")
    m = re.search(
        r"\*\*`\.claude/hooks/lib/preamble\.sh`\*\*\n\n```bash\n(.*?)```", doc, re.S
    )
    if not m:
        bad("could not locate the preamble block in the document")
        return
    with tempfile.TemporaryDirectory() as tmp:
        pre = Path(tmp) / "preamble.sh"
        pre.write_text(m.group(1))
        script = f"""
        source {pre}
        PAYLOAD='{{"flag":false,"name":"abc","nil":null}}'
        echo "false_field=$(field_opt '.flag' 'DEFAULT')"
        echo "absent=$(field_opt '.gone' 'DEFAULT')"
        field '.flag'  >/dev/null 2>&1; echo "field_false_rc=$?"
        field '.gone'  >/dev/null 2>&1; echo "field_absent_rc=$?"
        ( require jq __nope__ ) >/dev/null 2>&1; echo "require_rc=$?"
        ( die_closed x ) >/dev/null 2>&1; echo "die_rc=$?"
        ( warn_open  x ) >/dev/null 2>&1; echo "warn_rc=$?"
        """
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        got = dict(l.split("=", 1) for l in r.stdout.strip().splitlines() if "=" in l)

    want = {
        "false_field": (
            "false",
            "a legitimately-false field is not concatenated with its default",
        ),
        "absent": ("DEFAULT", "an absent field yields the default"),
        "field_false_rc": ("0", "field() does not treat false as schema drift"),
        "field_absent_rc": ("1", "field() reports an absent field"),
        "require_rc": ("2", "require() checks every argument, not just $1"),
        "die_rc": ("2", "die_closed exits 2 (blocking)"),
        "warn_rc": ("1", "warn_open exits 1 (non-blocking)"),
    }
    for key, (expected, label) in want.items():
        actual = got.get(key, "<missing>")
        ok(label) if actual == expected else bad(
            label, f"expected: {expected}\nactual:   {actual}"
        )


def check_external_links(doc: str) -> None:
    print("\nexternal links")
    urls = sorted(set(re.findall(r"\]\((https?://[^)]+)\)", doc)))
    for url in urls:
        r = subprocess.run(
            [
                "curl",
                "-sS",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "-L",
                "--max-time",
                "25",
                url,
            ],
            capture_output=True,
            text=True,
        )
        code = r.stdout.strip()
        ok(f"{code} {url[:64]}") if code.startswith(("2", "3")) else bad(
            f"{code} {url}"
        )


def main() -> int:
    doc = DOC.read_text()
    check_slug_algorithm()
    check_anchors(doc)
    check_fences(doc)
    check_bash_blocks(doc)
    check_preamble_behaviour(doc)
    if "--links" in sys.argv:
        check_external_links(doc)
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
