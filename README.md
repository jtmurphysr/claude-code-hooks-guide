# Claude Code Hooks: A Practical Guide

**Audience:** engineers running Claude Code on project work.
**Scope:** how hooks actually behave, how they fail, and how we review them.
**Status:** living doc. Hook surface moves fast — see [Verifying against your version](#verifying-against-your-version) before you trust any schema detail here.
**Last verified against the official hooks reference:** 2026-08-24.
**Worked examples:** the two hooks in §6 are wired and running in [`jtmurphysr/agent-harness`](https://github.com/jtmurphysr/agent-harness) — see [`.claude/hooks/`](https://github.com/jtmurphysr/agent-harness/tree/main/.claude/hooks) and the [`settings.json`](https://github.com/jtmurphysr/agent-harness/blob/main/.claude/settings.json) that wires them. Every figure quoted about them is measured from that repo, not estimated.

---

## TL;DR

- A hook is your code, run by Claude Code at a fixed lifecycle event, whether or not the model felt like cooperating.
- **Exit `2` blocks. Exit `1` does not.** This is the single most common bug in hooks written by people who know Unix.
- **A hook that can't run is a hook that isn't there.** Most broken guardrails in the wild don't throw — they silently return "allow." Write fail-closed or don't call it a guardrail.
- **A helper that exits from inside `$(...)` doesn't block anything.** It kills the subshell and the hook keeps going. See §4 — this bug shipped in an earlier draft of this very document.
- Use the lowest layer that solves the problem: `CLAUDE.md` → `permissions` → `if` on a handler → hooks → sandbox. Hooks are the middle band, not the whole answer.
- `.claude/` is executable code that ships in the repo. It goes through review like build scripts do.

---

## 1. Five layers of control

Pick the lowest one that works. Every layer up costs maintenance.

| Layer | What it is | Guarantee |
|---|---|---|
| `CLAUDE.md` / rules files | Instructions in the model's context | **None.** Competes for attention with everything else. Works most of the time, which is not a safety property. |
| `permissions` in `settings.json` | Declarative allow/ask/deny rules on tool calls | **Deterministic, no code.** Built in. Underused. |
| `if` on a handler (§2.4) | Permission-rule syntax matched against tool name *and arguments* | **Deterministic, no code**, but scoped to one handler. Does the argument parsing you'd otherwise hand-roll. |
| Hooks | Your program, run at a lifecycle event | **Deterministic, arbitrary logic.** Costs you a script to maintain and a failure mode to manage. |
| Sandbox / worktree / container | Blast-radius containment | **Catches what nobody modeled.** The only layer that helps with the failure you didn't imagine. |

Concretely: if you want to stop the agent reading `.env`, you do not need a hook.

```json
{
  "permissions": {
    "deny": ["Read(./.env)", "Read(./.env.*)", "Bash(curl:*)"]
  }
}
```

Note what that does and doesn't do: it denies the `Read` **tool**. It does not stop `Bash(cat .env)`. Tool-scoped rules are scoped to the tool — which is the whole argument of §5, arriving early.

Reach for a hook when the decision needs logic a rule can't express — parsing the payload, shelling out to a linter, checking repo state, injecting context.

> **Rule of thumb.** If your hook script is a `grep` against a fixed list, it probably wanted to be a permission rule, or the `if` field in §2.4.

---

## 2. The contract

Configuration lives in a `hooks` block keyed by event name, then matcher groups, then handlers:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/guard-bash.sh",
            "timeout": 15
          }
        ]
      }
    ]
  }
}
```

`$CLAUDE_PROJECT_DIR` expands to the repo root. Use it. Never write a hook that depends on `cwd`.

### 2.1 Matchers

For tool events (`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionRequest`, `PermissionDenied`), `matcher` filters on the **tool name only**. It never sees arguments. Other events match on other fields — `SessionStart` on the session origin, `PreCompact` on the trigger, and so on.

How the string is evaluated depends on which characters are in it:

| Matcher value | Evaluated as |
|---|---|
| `"*"`, `""`, or omitted | Match all — fires on every occurrence of the event |
| Only letters, digits, `_`, `-`, spaces, `,`, `\|` | Exact string, or a list of exact strings separated by `\|` or `,` |
| Contains any other character | JavaScript regular expression, **unanchored** |

Everything surprising about matchers falls out of that table:

- `Bash` is an exact match. It matches the `Bash` tool and **not** `BashOutput`.
- `Edit|Write` is an exact-match list, not a regex — same result here, different mechanism.
- `mcp__memory` is an exact string and matches nothing, because real tool names look like `mcp__memory__create_entities`. A whole server needs `mcp__memory__.*`, which lands in the regex row because of the `.` and `*`.
- Regexes are unanchored. `^Notebook$` if you mean it.
- Permission-rule syntax (`Bash(git *)`) in a `matcher` is a **silent no-op**: the parens push it into the regex row, and the resulting pattern can never match the bare tool name. That belongs in `if`.

Two version-and-event caveats on that table:

- **Hyphens moved.** Treating `-` as an exact-match character requires **v2.1.195+**; below that a hyphen forces the regex row. A matcher like `code-reviewer` therefore means different things on different installs — check `claude --version` before relying on it.
- **`FileChanged` and `StopFailure` use a narrower set.** Only letters, digits, `_`, and `|` stay on the exact-match path. A hyphen, space, or comma pushes them to regex, and only `|` separates alternatives.

### 2.2 Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. Structured JSON on stdout is parsed if present. |
| `2` | **Blocking.** stderr is fed back to the model as feedback. |
| anything else | **Non-blocking error.** The action proceeds. stderr shows up as a warning. |

Read that middle row twice. `exit 1` — the conventional Unix failure code — **blocks nothing**. If your policy hook exits 1 on failure, you have written a logger.

This is the most common way an existing script gets miswired. CI linters conventionally exit `1`. The structural linter in our own harness, [`scripts/validate_harness.py`](https://github.com/jtmurphysr/agent-harness/blob/main/scripts/validate_harness.py), documents exactly that: "Exits 0 if clean. Exits 1 with actionable output on violations." Correct for CI, inert as a hook. Wrapping is the fix, not editing the linter — see §6.3.

**Not every event honours that table.** `WorktreeCreate` fails creation on *any* nonzero exit, and events differ in whether they can block at all: `PostToolUse` never blocks (the tool already ran) but does show stderr to the model, while `PostToolBatch` blocks the agentic loop before the next model call. Check the reference for the event you're on rather than assuming this table is universal.

### 2.3 Structured output

For finer control, print JSON on stdout and exit `0`.

Top-level fields available on every event:

| Field | Effect |
|---|---|
| `continue` | `false` stops Claude processing entirely after the hook runs. Takes precedence over event-specific decisions. This is how you hand control to a human. |
| `systemMessage` | Shown in the transcript. |
| `additionalContext` | Injected into the model's context. Discarded by events that don't accept it. |

Per-event decisions live under `hookSpecificOutput`. For `PreToolUse`:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "ask",
    "permissionDecisionReason": "Writes to infra/ need a human. Confirm this is intended."
  }
}
```

- `permissionDecision` is `allow`, `deny`, or **`ask`**.
  - `deny` blocks; the model sees `permissionDecisionReason`.
  - `ask` escalates to the normal user permission prompt. **For most policy hooks this is the right answer, not `deny`.** A gate that stops the human as well as the agent gets switched off in week three.
  - `allow` **skips the permission prompt entirely.** An over-broad `allow` hook is a larger hole than any `deny` hook is a patch. Do not write one to reduce prompt fatigue.
- `permissionDecisionReason` is read by the model. Write it as an instruction, not a verdict — "use `rg`, not `grep`, in this repo" recovers the turn; "Denied." tends to stall it.
- `updatedInput` replaces fields in `tool_input`, letting the hook **rewrite** the call instead of refusing it — force a `--dry-run`, redirect a path, add `-i`. Frequently better than blocking: the agent keeps moving and the dangerous shape is gone.

Shapes are **not uniform across events** — check the reference for the event you're using. Older documentation and most blog posts show a flat top-level `decision` field; treat any example using it as unverified against your version.

Hook output strings — stdout, `additionalContext`, `systemMessage` — are capped at **10,000 characters**. Past that it's written to a file and replaced with a preview, which is not what you want a failing test log to do.

### 2.4 `if`: the layer between a permission rule and a script

Each individual handler takes an `if` field using **permission-rule syntax**, matched against the tool name *and its arguments*. This is what `matcher` cannot do.

```json
{
  "matcher": "Bash",
  "hooks": [
    {
      "type": "command",
      "if": "Bash(git push --force*)",
      "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/guard-force-push.sh",
      "timeout": 10
    }
  ]
}
```

`if` does the parsing you would otherwise hand-roll badly: it evaluates Bash subcommands, `$()`, and backticks, and strips leading `VAR=value` assignments before matching. `Edit(*.ts)` runs a handler only for TypeScript files.

Half the hook scripts in the wild — the ones that `jq` out `.tool_input.command` and `grep` it — are an `if` clause with extra failure modes. Reach for it before writing a script.

It is only evaluated on tool events. **On any other event, a handler with `if` set never runs at all.**

### 2.5 Four things that catch people

1. **`PostToolUse` cannot undo the edit.** It already ran. Exit `2` feeds your stderr to the model so it fixes the code; it does not roll anything back.
2. **`Stop` exit `2` forces the agent to keep working.** This is how you gate "done."
3. **Handler types beyond `command` have sharp edges.** There are five: `command`, `http`, `mcp_tool`, `prompt`, and `agent`. HTTP hooks can't block via status code — a 500 is a non-blocking error, so a dead endpoint fails open. `mcp_tool` inherits the availability of the server behind it. `prompt` and `agent` put a model in the decision path, which is nondeterministic by construction and experimental in the case of `agent`. **For anything policy-shaped, use `command`.**

4. **`command` has two forms, and the safer one is less known.** Supplying `args` uses exec form — the command is invoked directly, with no shell interpretation of its arguments. Omit `args` and you get shell form, where anything interpolated from the payload is shell-parsed. Given §8, prefer exec form whenever you pass values you didn't author. A `shell` field selects `bash` or `powershell` when you do want shell form.

---

## 3. The four ways a hook goes quiet

This is the section that matters. A hook rarely fails loudly. It fails by returning "allow" and letting you believe you're protected.

### 3.1 Missing dependency

The canonical published example — the one that shows up in every blog post — looks like this:

```bash
# DO NOT COPY THIS
set -uo pipefail
input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty')
if printf '%s' "$cmd" | grep -Eq 'rm[[:space:]]+-[a-zA-Z]*r'; then
  # ... deny ...
fi
exit 0
```

No `jq` on `$PATH` → `cmd` is empty → grep doesn't match → `exit 0` → the guardrail allows everything, forever, silently. `// empty` is the fail-open idiom. It should be treated as a code smell in any hook that makes a policy decision.

### 3.2 Payload shape drift

`tool_input` fields change between versions. A `jq` path that stops resolving degrades to the same empty-string silence as a missing binary. Assert the field exists; don't default it.

### 3.3 Timeout

A hook that exceeds its `timeout` is cancelled. **Cancelled is not blocked.** If your `Stop` gate runs a 12-minute suite under a 60-second timeout, your gate does not exist. Set `timeout` explicitly on anything that shells out.

When the honest timeout is longer than you're willing to block for, `asyncRewake` is the escape hatch: the hook runs in the background and **wakes Claude on exit `2`**, surfacing its stderr as a system reminder. You trade synchronous blocking for a late interrupt — the agent may already have moved on — so it fits long verification suites, not policy gates.

```json
{ "type": "command", "command": "...", "asyncRewake": true }
```

Plain `"async": true` runs in the background and never blocks at all; it implies nothing about waking. Use it for the observe-tier hooks in §4's table and nothing else.

### 3.4 Wrong exit code, or the right exit code in the wrong process

`exit 1` proceeds — every policy path must exit `2`.

And the subtler one, which is §4's whole subject: `exit 2` from inside a command substitution exits the *subshell*. The hook continues to its own `exit 0` and allows the call. Your stderr will even say "BLOCKED."

---

## 4. House pattern: fail-closed by default

> **This section had the bug it warns about.** An earlier draft's `field()` helper called `die_closed` (which does `exit 2`) from inside `file=$(field '.tool_input.file_path')`. Command substitution runs in a subshell, `set -e` was not enabled, and so the hook printed `BLOCKED` to stderr and then **allowed the write and exited 0**. Verified behaviour, not theory. The rule below exists because of it.
>
> **Contract: helpers never exit. They return non-zero. The call site decides.**

That contract is a simplification, and the honest version is worth stating because the gap is where the original bug lived. There are **two classes of helper**, and they cannot be collapsed into one rule — `die_closed` has to exit; that *is* the block:

| Class | Members | Safe inside `$( )`? |
|---|---|---|
| **Returning** — report failure to the caller | `field`, `field_opt`, `read_payload` | **Yes.** That's the point. |
| **Exiting** — terminate the hook | `die_closed`, `warn_open`, `require`, `decide`, `fail` | **No. Top-level only.** |

A convention that lives only in a comment gets violated in month four by someone reasonably assuming a helper is a helper. Two things make it stick. **Name the classes so the distinction is visible at the call site** — a returning helper is always `x=$(f …) || die_closed`, an exiting one is never on the right of an `=`. And **enforce it in CI**, because `shellcheck` will not catch this:

```bash
# Fails if a returning helper is captured without a || guard.
grep -nE '\$\((field|field_opt|read_payload)\b[^)]*\)\s*$' .claude/hooks/*.sh \
  && { echo "unguarded capture — needs || die_closed"; exit 1; }

# Fails if an exiting helper is used in a substitution at all.
grep -nE '\$\((die_closed|warn_open|require|decide|fail)\b' .claude/hooks/*.sh \
  && { echo "exiting helper inside \$( ) — exits the subshell only"; exit 1; }
```

That second grep is the one that would have caught the bug this section is named after.

Source this preamble at the top of every hook that makes a decision.

**`.claude/hooks/lib/preamble.sh`**

```bash
#!/usr/bin/env bash
# Shared hook preamble. Source, don't exec.
#
# CONTRACT: helpers that can fail RETURN non-zero; they never exit. Exiting from
# inside a command substitution -- x=$(field ...) -- only kills the subshell and
# the hook sails on to exit 0. Every call site below must end in `|| die_closed`.
set -uo pipefail

HOOK_NAME="${HOOK_NAME:-$(basename "${BASH_SOURCE[1]:-hook}")}"

# Terminal, and only ever called from the top level of a hook script.
# A guardrail that cannot do its job must block, not shrug.
die_closed() {
  echo "[$HOOK_NAME] BLOCKED — guard could not evaluate: $*" >&2
  exit 2
}

# For advisory hooks only. Use deliberately, never as a fallback.
warn_open() {
  echo "[$HOOK_NAME] warning: $*" >&2
  exit 1
}

# Top-level only, so exiting here is safe. Takes any number of deps and
# reports all the missing ones -- a single-argument version silently ignores
# `require jq cksum`, which is the §3.1 bug living inside the guard itself.
require() {
  local missing=() dep
  for dep in "$@"; do
    command -v "$dep" >/dev/null 2>&1 || missing+=("$dep")
  done
  [ ${#missing[@]} -eq 0 ] || die_closed "missing dependencies: ${missing[*]}"
}

# Returns 1 on empty payload.  Call: PAYLOAD=$(read_payload) || die_closed "..."
read_payload() {
  local p
  p=$(cat)
  [ -n "$p" ] || return 1
  printf '%s' "$p"
}

# jq -e exits non-zero on null/false, so a missing field is an error, not "".
# Returns jq's status.  Call: file=$(field '.a.b') || die_closed "..."
field() {
  jq -er "$1" <<<"$PAYLOAD" 2>/dev/null
}

# For fields that are legitimately absent rather than schema drift. This is the
# ONE place a default is correct -- everywhere else `// empty` is the fail-open
# smell from §3.1.  Call: flag=$(field_opt '.stop_hook_active' 'false')
field_opt() {
  jq -er "$1" <<<"$PAYLOAD" 2>/dev/null || printf '%s' "${2-}"
}

# Emit findings to the model and block. Top-level only.
# `tail`, because output past 10k chars becomes a file path, not feedback.
fail() {
  { echo "$1"; shift; printf '%s\n' "$@" | tail -40; } >&2
  exit 2
}

# Emit a PreToolUse decision and exit 0.  decide deny|ask|allow "reason"
# Built with jq so the reason is escaped correctly no matter what's in it.
decide() {
  jq -nc --arg d "$1" --arg r "$2" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:$d,permissionDecisionReason:$r}}'
  exit 0
}
```

**`.claude/hooks/guard-write-paths.sh`** — example: protected paths.

```bash
#!/usr/bin/env bash
set -uo pipefail

# BOOTSTRAP GUARD — see the warning under this block. die_closed does not exist
# until the source succeeds, and every shorthand for "fail if unset" exits 1.
if [ -z "${CLAUDE_PROJECT_DIR:-}" ]; then
  echo "[guard-write-paths] BLOCKED — CLAUDE_PROJECT_DIR unset" >&2
  exit 2
fi
source "$CLAUDE_PROJECT_DIR/.claude/hooks/lib/preamble.sh"

require jq
PAYLOAD=$(read_payload)                        || die_closed "empty stdin payload"
file=$(field '.tool_input.file_path')          || die_closed "no .tool_input.file_path (schema drift?)"

# Normalise to a repo-relative path. Claude Code sends absolute paths for Edit/Write
# today; don't build the policy on an assumption you haven't asserted.
rel="${file#"$CLAUDE_PROJECT_DIR"/}"

# ESCAPE CHECK, and it has to come first. If the strip was a no-op then the path
# is not under the repo at all -- rel is still absolute, no case arm below can
# match, and the hook would exit 0. That is how a guard whose job is protecting
# .claude/ waves through ~/.claude/settings.json and ~/.ssh/config.
if [ "$rel" = "$file" ]; then
  decide ask "${file} is outside the project directory. Confirm this is intended."
fi

case "$rel" in
  .claude/*|.github/workflows/*)
    # Self-modification of the guardrail, or of CI. Never automatic.
    decide deny "Agents do not edit ${rel}. Describe the change in the PR body; a human will make it."
    ;;
  infra/*)
    # Legitimate often enough that a hard deny would just get the hook deleted.
    decide ask "${rel} is infrastructure. Confirm this change is intended."
    ;;
esac

exit 0
```

Wire it:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|NotebookEdit",
        "hooks": [
          { "type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/guard-write-paths.sh", "timeout": 10 }
        ]
      }
    ]
  }
}
```

(Check that alternation against your version's actual tool set before copying — matchers are exact strings, so a tool name that no longer exists is dead weight and a new one is an uncovered gap.)

> **The bootstrap is the one place this apparatus can't defend itself.** Every guard above depends on `die_closed`, which lives in the preamble, which is located through `$CLAUDE_PROJECT_DIR`. If that variable is unset, the failure happens *before* the tool to handle it exists — and every shorthand lands on the wrong exit code:
>
> | Written as | Exit | Effect |
> |---|---|---|
> | `source "$CLAUDE_PROJECT_DIR/..."` under `set -u` | `1` | **allowed** |
> | `source "${CLAUDE_PROJECT_DIR:?unset}/..."` | `1` | **allowed** |
> | explicit `[ -z … ] && { echo >&2; exit 2; }` | `2` | blocked |
>
> `set -u` and `:?` both exit **1**, which is non-blocking. A fail-closed guard that hits this fails open, silently, on the one condition it cannot report. Write the check by hand, above the `source`, in every hook that blocks. (Both figures verified, not assumed — and this was live in the [harness](https://github.com/jtmurphysr/agent-harness/blob/main/.claude/hooks/gate-done.sh) until a reviewer caught it.)

### Choosing fail-open vs fail-closed

Decide per hook, and write the choice down in a comment.

| Hook's job | Behavior when the hook itself errors |
|---|---|
| Prevent (policy, secrets, protected paths) | **Fail closed.** `exit 2`. |
| Verify (security analysis, type check) | **Fail closed.** "Analysis didn't run" is not "code is clean." |
| Format / style | Fail open (`exit 1`). Cosmetic. |
| Observe (logging, push notification, metrics) | Fail open, and mark it `"async": true` so it never stalls the loop. |

> The test: **fail-open is only acceptable for a hook whose permanent absence you'd accept.** If you wouldn't ship without it, it fails closed.

And separately from the error path, decide the *success* path: when the hook works correctly and the answer is "no," is that a `deny` or an `ask`? Default to `ask` unless a human could never legitimately want this.

---

## 5. What a hook can't see

`PreToolUse` on `Bash` hands you the command **as text, before expansion**. You are pattern-matching intent, not inspecting a syscall. That is a permanent ceiling, not a curation problem.

Everything below walks past a `rm -rf` denylist:

```
find . -type f -delete
git clean -xfd
rm -r --force ~/
X=$(echo "rm"); $X -rf ~/
printf 'rm -rf ~/\n' > cleanup.sh && bash cleanup.sh
```

That last one is the important shape: the hook sees `bash cleanup.sh`. The agent wrote the destructive command through a tool your matcher wasn't watching, then executed a filename.

**Implications:**

- Denylists catch the failure you already imagined. They do nothing about the one you didn't.
- Prefer **allowlisting command heads** over enumerating forbidden ones, and prefer `permissions` rules and `if` clauses over both — at least those parse `$()` and backticks properly instead of regexing a string.
- Then stop pretending. Run agentic work in a git worktree, a container, or a VM. A band of determinism is not a box.

---

## 6. What's actually worth hooking

Three patterns earn their maintenance. The rest is usually a permission rule or a `CLAUDE.md` line.

### 6.1 `SessionStart` — inject the context that must always be present

Project conventions, current branch state, the layer taxonomy, the `project_context.md`. Returning `additionalContext` beats hoping the model reads a file. Mind the 10,000-character cap.

**In the harness:** `.claude/hooks/inject-context.sh` injects `AGENTS.md`, the operating constitution. That file opens with "read it in full before taking **any** action" — a request, not a guarantee. The hook makes the load-bearing half unconditional.

It also demonstrates the cap. `AGENTS.md` is **10,278 characters against a 10,000 limit**, so injecting it whole would be written to a file and replaced with a preview — strictly worse than not running. The hook selects five sections (Repository Identity, Module Boundaries, Critical Agent Warnings, Harness Lessons, Definition of Done) and emits 6,671 characters.

Select by **heading name, not byte offset**, or the hook silently starts injecting the wrong half the first time someone edits the document:

```bash
WANTED='Repository Identity|Module Boundaries|Critical Agent Warnings|Harness Lessons|Definition of Done'
excerpt=$(awk -v want="$WANTED" '
  /^## / { title = substr($0,4); sub(/[ \t]+$/,"",title); keep = 0
           n = split(want,w,"|"); for (i=1;i<=n;i++) if (index(title,w[i])==1) keep = 1 }
  keep   { print }' "$AGENTS")
```

That hook **fails open**, deliberately and in a comment. A session missing its preamble is degraded, not unsafe — and `SessionStart` cannot block anyway, so failing closed there would be theater. Guardrails belong on `PreToolUse` and `Stop`.

> Plain stdout also becomes context on `SessionStart`, `UserPromptSubmit`, and `UserPromptExpansion` — the three events where exit-0 stdout is injected rather than merely logged. `additionalContext` is still preferable when you want the JSON envelope for `systemMessage` alongside it.

### 6.2 `PostToolUse` — verify what was written, while the agent is still in the loop

Fires immediately after each `Edit`/`Write`, long before CI. Exit `2` with findings on stderr and the model fixes it before doing anything else.

Note what `exit 2` means *here*: `PostToolUse` cannot block — the write already happened — but stderr is shown to the model as feedback. You are steering the next turn, not preventing this one.

> **Per-write is not always the right granularity.** When parallel edits should be judged together — a rename that only type-checks once every call site lands — `PostToolBatch` fires after a whole batch resolves, and its `exit 2` genuinely stops the agentic loop before the next model call. Reach for it when per-file checks would fight each other.

**Loop guard required, and the guard must actually terminate.** Exit `2` → model edits → hook fires again → exit `2`. If your check is nondeterministic or unfixable, that's an infinite loop burning tokens. Note that escalating with *another* `exit 2` doesn't break the cycle — it's still a block, you've only changed the wording. Stop the turn with `continue: false`:

```bash
require jq cksum
PAYLOAD=$(read_payload)               || die_closed "empty stdin payload"
sid=$(field '.session_id')            || die_closed "no .session_id"
tgt=$(field '.tool_input.file_path')  || die_closed "no .tool_input.file_path"

# Per session AND per file: three unrelated failures shouldn't park the gate
# in escalation for the rest of the session. Filename key only, not a secret.
key=$(printf '%s|%s' "$sid" "$tgt" | cksum | tr -d ' ')
COUNT_FILE="${TMPDIR:-/tmp}/cc-verify-$key"

if findings=$(run_verify "$tgt"); then
  rm -f "$COUNT_FILE"          # reset on success, or the counter only ever climbs
  exit 0
fi

n=$(( $(cat "$COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$COUNT_FILE"

if [ "$n" -gt 3 ]; then
  rm -f "$COUNT_FILE"
  jq -nc '{continue:false,stopReason:"verify gate: 3 failed attempts on this file — needs a human"}'
  exit 0
fi

{ echo "verify failed (attempt $n/3):"; echo "$findings" | tail -30; } >&2
exit 2
```

### 6.3 `Stop` — gate "done" on evidence, not on the model's opinion

This is the highest-value hook we run. The model declaring completion is a claim. The suite passing is a receipt.

```bash
#!/usr/bin/env bash
set -uo pipefail
source "$CLAUDE_PROJECT_DIR/.claude/hooks/lib/preamble.sh"
require jq
PAYLOAD=$(read_payload) || die_closed "empty stdin payload"

# Prevents an infinite Stop loop: set when we're already continuing from a Stop
# hook. Legitimately absent on the first pass, so field_opt (not field) is right.
[ "$(field_opt '.stop_hook_active' 'false')" = "true" ] && exit 0

if ! out=$(make test 2>&1); then
  # tail, not the whole log: hook output is truncated to a file past 10k chars,
  # and a preview path is not useful feedback to the model.
  { echo "Test suite is failing. You are not done."; echo "$out" | tail -30; } >&2
  exit 2
fi
exit 0
```

Wire it with a timeout that fits the suite. The default is generous but not infinite, and §3.3 applies with full force here — a cancelled `Stop` gate is an absent `Stop` gate:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/gate-done.sh", "timeout": 900 }
        ]
      }
    ]
  }
}
```

**In the harness:** `.claude/hooks/gate-done.sh` runs `scripts/validate_harness.py` (module-boundary invariants, stdlib-only, fast) and then `pytest`. Cheapest check first, so a boundary violation doesn't wait on a full suite. It mirrors what CI runs — a gate that disagrees with CI just relocates the argument.

Three things that section is worth reading for.

**1. The exit-code translation is the entire point.** `validate_harness.py` is a CI linter, and its docstring says so:

```
Exits 0 if clean. Exits 1 with actionable output on violations.
```

Correct for CI. Wire it into a hook as-is and **exit 1 blocks nothing** — the gate silently passes every violation it finds, while looking like it works. Same script, correct in one context and inert in the other:

```bash
if ! harness_out=$("$PY" scripts/validate_harness.py 2>&1); then
  fail "Module boundary / coverage violations. You are not done." "$harness_out"   # fail() exits 2
fi
```

**2. "Couldn't run" and "failed" need different messages.** Fail-closed is right — "the tests did not run" is not "the tests passed" — but on a fresh checkout that means the agent is blocked forever by a wall of `ImportError`. Split on pytest's exit code (2/3/4 = interrupted, internal, usage) and return the fix instead:

```bash
case "$rc" in
  0) ;;
  5) fail "pytest collected no tests — the gate cannot verify anything." "$test_out" ;;
  2|3|4)
    fail "Test suite could not run (pytest rc=$rc) — the gate could not verify this work.
If this is a fresh checkout, the environment is not provisioned:
    pip install -e \".[dev]\"
Fail-closed by design: 'the tests did not run' is not 'the tests passed.'" "$test_out" ;;
  *) fail "Test suite is failing. You are not done." "$test_out" ;;
esac
```

This is §3.1's missing-dependency failure wearing the opposite costume: a missing `jq` makes a guard fail *open*; a missing test dependency makes a gate fail *permanently closed*. Both are the hook not doing its job, and neither announces itself.

**3. `stop_hook_active` is not optional.** Without it, the hook's own `exit 2` re-triggers `Stop` and loops forever:

```bash
[ "$(field_opt '.stop_hook_active' 'false')" = "true" ] && exit 0
```

**Test all three paths before trusting a gate**, because the failure modes are the point:

| Scenario | Expected |
|---|---|
| Green tree | exit `0` |
| Checks fail | exit `2`, findings on stderr |
| `stop_hook_active: true` | exit `0`, no loop |

A gate verified only against a red tree is a gate you have never seen let work through.

> **The architect reviewer deliberately does not run here.** It is PR-bound by construction — `review_pr(repo, pr_number)` taking a `PRDiff` — and a `Stop` hook fires on a working tree that frequently has no PR at all. Beyond feasibility, a model verdict inside a blocking gate is the §6.2 anti-pattern with tokens attached: BLOCK → edit → BLOCK, with no guarantee the verdict is stable across identical code. Design review stays on the PR, where there is a diff and a human. **Gate on receipts, review on pull requests.**

### 6.4 Notifications

A notification hook is an **egress path** that runs on an engineer's machine with that engineer's network access. Pick the destination on that basis, not on convenience.

What we run: a **self-hosted [ntfy](https://ntfy.sh) instance** — private, authenticated, on our own infrastructure — with the token in `settings.local.json`, never committed. Self-hosting is the point rather than a preference: the events a hook emits describe what an agent is doing inside your repo, and a third-party SaaS webhook makes that someone else's log to retain, breach, or subpoena.

Use whatever you like — ntfy, Slack, Webex, a webhook of your own. Two rules survive the choice: **the token lives in local settings**, and **a destination you don't control is a data-handling decision**, not a formatting one.

---

## 7. Team distribution and settings precedence

This is what makes hooks work for a group instead of one laptop.

| Source | Committed? | Use for |
|---|---|---|
| Enterprise managed policy settings | Managed | Org-mandated hooks. Cannot be disabled by user/project/local `disableAllHooks`. |
| `.claude/settings.json` | **Yes** | Team guardrails everyone gets on clone. The default home for anything in this guide. |
| `.claude/settings.local.json` | **No** (gitignore it) | Personal preferences, local paths, tokens, noise. |
| `~/.claude/settings.json` | N/A | Your own cross-project habits. **Not read in cloud sessions** — see below. |
| Plugin `hooks/hooks.json` | Via the plugin | Hooks that arrive with an enabled plugin. Audit these like any dependency. |
| Skill / subagent frontmatter | Yes | Scoped hooks that exist only while that skill or subagent is active. |

Hooks **merge** across these sources rather than replacing each other. `disableAllHooks` at any level turns off everything non-managed; `allowManagedHooksOnly` is the org-side switch that reduces the set to managed policy hooks only.

**Conventions worth adopting:**

1. Team guardrails go in `.claude/settings.json` and are committed. If it's not committed, it isn't a guardrail — it's your personal preference.
2. **Cloud and CI sessions do not read `~/.claude/settings.json`.** Claude Code on the web and headless/CI runs see repo `.claude/settings.json` plus org managed settings, and nothing else. A guardrail that lives in your home directory is a guardrail that silently does not exist the moment the same work runs somewhere else — which is exactly the fail-quiet class in §3. Anything you actually depend on goes in the repo.
3. Hook scripts live in `.claude/hooks/`, are executable, and have a shebang.
4. `.claude/settings.local.json` is in `.gitignore`. Verify this before your first commit.
5. Add `.claude/` to `CODEOWNERS`. Changes to hooks get the same review as changes to CI.
6. Hooks are **hot-reloaded** — a mid-session edit to a settings file is picked up by the file watcher. Don't assume config is frozen at startup, and don't debug against a stale mental model.
7. `/hooks` in the CLI is a read-only inspector: it shows every configured hook and which settings file it came from. First stop when behavior surprises you.

---

## 8. `.claude/` is an attack surface

The mechanism that lets you customize the agent lets a repo author customize it too.

- Hook definitions live in files **inside the repository**. Cloning a repo and opening it can execute someone else's code.
- Command hooks run **unsandboxed, with your full user privileges** — your `$PATH`, your env, your credentials, your network, your filesystem. There is no isolation layer.
- The control that's supposed to stand between you and that is **workspace trust**: hooks defined in a project's `.claude/settings.json` are not registered until you accept the trust dialog for that folder. Note the scope of that protection — hooks from your own user settings, from managed policy, and from plugins are not behind it, and project *skill* frontmatter hooks register when the skill is invoked.
- That control has failed before. [CVE-2025-59536](https://github.com/advisories/GHSA-4fgq-fpq9-mr3g) (CVSS 8.7, fixed in **1.0.111**): opening an untrusted project was remote code execution. Per [Check Point's writeup](https://research.checkpoint.com/2026/rce-and-api-token-exfiltration-through-claude-code-project-files-cve-2025-59536/), the vector was a `SessionStart` event with a `startup` matcher in a cloned repo's `.claude/settings.json`, which ran "immediately, with no additional prompt or execution warning." The GitHub advisory states only that code could execute before the trust dialog was accepted; the hook-level detail comes from Check Point.

- **And it is not only hooks.** The companion flaw, CVE-2026-21852, needed no hook at all: a repo shipped a settings file setting **`ANTHROPIC_BASE_URL`** to an attacker-controlled endpoint, and Claude Code issued API requests against it *before* the trust prompt — leaking the user's API key and redirecting authenticated traffic to attacker infrastructure. A plain configuration key, no executable stanza. Reported fixed in 2.0.65 (CVSS 5.3; both figures are from secondary sources, unlike the 8.7 above).

> Read that pair together and the section title undersells the problem. **`.claude/settings.json` is the attack surface**; hooks are the loudest thing in it, not the only dangerous one. A review that greps for `"hooks"` and stops has missed the second CVE entirely.

**Practice:**

- Keep Claude Code patched. Check `claude --version` against the changelog before assuming a CVE doesn't apply to you.
- Never open an untrusted repo with hooks unexamined. Read `.claude/settings.json` first, or open it somewhere disposable. Treat the trust dialog as a control that has been bypassed once, not as a guarantee.
- **Read the whole settings file, not just the `hooks` block.** `env`, `ANTHROPIC_BASE_URL`, `permissions.allow`, and MCP server definitions all change where your credentials and traffic go, and none of them look like code.
- Treat `.claude/` as security-sensitive code: version controlled, reviewed in PRs, owned in `CODEOWNERS`.
- Because hook config runs on engineer endpoints, changes to `.claude/` in customer-facing or regulated project repos should route through the same review path as build tooling. Flag anything that phones home.

---

## 9. Review checklist for PRs touching `.claude/`

- [ ] Every dependency is asserted with `require`, not assumed.
- [ ] No `// empty` or `|| true` on a path that makes a policy decision.
- [ ] **No helper exits from inside a command substitution.** Every fallible helper is called as `x=$(helper ...) || die_closed "..."`. Enforced by the §4 greps in CI, not by this line.
- [ ] Any hook that blocks checks `CLAUDE_PROJECT_DIR` **explicitly, before the `source`**, and exits `2`. `set -u` and `${VAR:?}` both exit 1, which allows the call.
- [ ] A path-based guard rejects paths that escape the project directory. If the prefix strip was a no-op, no `case` arm matches and the hook exits 0 — `~/.ssh/config` is outside every pattern you wrote.
- [ ] Every policy failure exits **`2`**, not `1`.
- [ ] The fail-open/fail-closed choice is deliberate and commented.
- [ ] Someone asked whether `ask` would serve better than `deny` on the success path.
- [ ] No hook returns `permissionDecision: "allow"` to reduce prompt fatigue.
- [ ] `timeout` is set explicitly on anything that shells out or runs a suite — and it is longer than the thing it runs. If it can't be, the hook is `asyncRewake`, not synchronous.
- [ ] An existing CI script wired in as a hook has its `exit 1` translated to `exit 2`. Linters exit 1 by convention; hooks ignore it.
- [ ] The "checks could not run" path is distinguishable from "checks failed," and says how to fix the environment.
- [ ] The gate was tested on a **green** tree, not only a red one. A gate nobody has watched succeed is a gate that may never succeed.
- [ ] `matcher` is the **tool name only**, and the exact-string vs regex behaviour (§2.1) is what the author intended. Argument filtering uses `if`, never `matcher`.
- [ ] Blocking hooks that feed the model have a loop guard whose terminal state is `continue: false` — not another `exit 2`, which just re-enters the loop.
- [ ] Any counter or state file is keyed narrowly and reset on success.
- [ ] Hook output stays under 10,000 characters (`tail` your logs).
- [ ] No secrets in committed settings. Local-only config is in `settings.local.json` and gitignored.
- [ ] The **non-hook** keys in the settings diff were read too — `env`, `ANTHROPIC_BASE_URL`, `permissions.allow`, MCP servers. CVE-2026-21852 was a config key, not a hook.
- [ ] Anything the team depends on is in `.claude/settings.json`, not `~/.claude/settings.json` — or it won't exist in cloud/CI sessions.
- [ ] The hook was tested against a **real** payload from `claude --debug`, not an assumed schema.
- [ ] Someone asked whether a `permissions` rule or an `if` clause would have done this with no script.

---

## Verifying against your version

Claude Code currently exposes roughly thirty lifecycle events, and the set grows between releases. Five carry nearly all real work: `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SessionStart`.

Do not write `jq` against a schema you read in a blog post — including this one.

```bash
claude --version          # know what you're running
claude --debug            # observe the actual payloads your version emits
# then /hooks in-session to inspect what's configured and where it came from
```

Where this document and the official hooks reference disagree, **the reference wins**. If you find a divergence, fix it here and open a PR.

---

## The part hooks can't do

Hooks enforce mechanics. They can gate a merge on a passing suite, block a write to a protected path, and refuse a command that matches a policy. All of that is delegable, and all of it should be.

What they cannot do is hold the seat where a human decides the work was the right work to do. Gating "the tests pass" is not the same as ratifying "this is what we meant to build." Every hook in this guide moves accountability *around*; none of them moves it *off*.

Build the mechanics so that the only thing left requiring a person is the thing that always required one.
