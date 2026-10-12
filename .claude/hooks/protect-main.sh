#!/usr/bin/env bash
# PreToolUse hook (Bash): blocks `git commit` on main/master and any `git push`
# that would update main/master. Every change goes through a feature branch + PR.
# Requires: jq.
set -uo pipefail

input=$(cat)
cmd=$(jq -r '.tool_input.command // empty' <<<"$input")
cwd=$(jq -r '.cwd // empty' <<<"$input")
[[ -z "$cmd" ]] && exit 0

deny() {
  jq -n --arg reason "$1" '{hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: $reason
  }}'
  exit 0
}

branch=$(git -C "${cwd:-.}" branch --show-current 2>/dev/null || true)
on_protected=false
[[ "$branch" == "main" || "$branch" == "master" ]] && on_protected=true

# Split compound commands so each git invocation is checked on its own.
while IFS= read -r segment; do
  if [[ "$segment" =~ (^|[[:space:]])git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+commit([[:space:]]|$) ]]; then
    $on_protected && deny "Committing on '$branch' is not allowed. Create a branch first (see the Git workflow section in CLAUDE.md)."
  fi

  if [[ "$segment" =~ (^|[[:space:]])git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+push([[:space:]]|$)(.*) ]]; then
    args="${BASH_REMATCH[4]}"
    if [[ "$args" =~ (^|[[:space:]:+]|refs/heads/)(main|master)([[:space:]]|$) || "$args" =~ --(all|mirror) ]]; then
      deny "Pushing to main/master is not allowed. Push a feature branch and open a pull request."
    fi
    # No explicit refspec means "push the current branch".
    positional=$(tr ' ' '\n' <<<"$args" | grep -vE '^(-|$)' | wc -l | tr -d ' ')
    if $on_protected && (( positional < 2 )); then
      deny "Pushing from '$branch' is not allowed. Create a feature branch and push that instead."
    fi
  fi
done < <(sed -E 's/(&&|\|\||;|\|)/\n/g' <<<"$cmd")

exit 0
