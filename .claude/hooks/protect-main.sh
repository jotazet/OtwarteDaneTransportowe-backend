#!/usr/bin/env bash
# PreToolUse hook (Bash): a local guard for the git workflow in CLAUDE.md.
#
# On main/master it denies git commit, merge, cherry-pick, revert, rebase, am and a
# `git pull` that would merge (`--no-ff`, or a branch other than main/master).
# Anywhere it denies a `git push` that would update main/master, and asks before
# pushing tags matching v* or creating a GitHub release: a release tag deploys to
# production.
#
# It only knows these command shapes. It does not see git run by scripts, aliases
# or `bash -c`, and it takes the branch from the working directory when the command
# starts (a `git switch main`/`checkout main` earlier in the same command counts).
# Requires: jq.
set -uo pipefail

input=$(cat)
# Quoted strings become one word whose spaces and shell operators are \x01, so
# `echo "a; git commit"` stays a single echo and `git push "main"` still reads main.
cmd=$(jq -r '(.tool_input.command // "")
  | gsub("\"(?<d>([^\"\\\\]|\\\\.)*)\"|'"'"'(?<s>[^'"'"']*)'"'"'";
         (.d // .s) | gsub("[\\s;&|()`]"; "\u0001"))' <<<"$input")
[[ "$cmd" == *git* || "$cmd" == *gh* ]] || exit 0
cwd=$(jq -r '.cwd // empty' <<<"$input")

decide() {  # decide <deny|ask> <reason>
  jq -n --arg decision "$1" --arg reason "$2" '{hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: $decision,
    permissionDecisionReason: $reason
  }}'
  exit 0
}
deny() { decide deny "$1"; }

is_protected() { [[ "$1" == main || "$1" == master ]]; }

ask_reason=""
switched=""  # branch a `git switch`/`checkout` earlier in the command moved to

while IFS= read -r segment; do
  read -ra w <<<"$segment"
  n=${#w[@]}
  i=0
  # Skip variable assignments and words that only prefix the command.
  while ((i < n)) && [[ "${w[i]}" =~ ^[A-Za-z_][A-Za-z0-9_]*= ||
    "${w[i]}" =~ ^(env|command|exec|time|then|do|else|\{|!)$ ]]; do
    ((i++))
  done
  ((i < n)) || continue

  if [[ "${w[i]}" == gh ]]; then
    [[ "${w[i + 1]:-}" == release && "${w[i + 2]:-}" == create ]] &&
      ask_reason="Creating a GitHub release pushes a tag, which deploys to production."
    continue
  fi
  [[ "${w[i]}" == git || "${w[i]}" == */git ]] || continue
  ((i++))

  # Global options before the subcommand; keep those that pick the repository.
  repo_opts=()
  while ((i < n)) && [[ "${w[i]}" == -* ]]; do
    case "${w[i]}" in
      -C | --git-dir | --work-tree)
        value=${w[i + 1]:-}
        repo_opts+=("${w[i]}" "${value//$'\x01'/ }")
        ((i += 2))
        ;;
      --git-dir=* | --work-tree=*)
        repo_opts+=("${w[i]//$'\x01'/ }")
        ((i++))
        ;;
      -c | --namespace | --config-env | --super-prefix | --attr-source) ((i += 2)) ;;
      *) ((i++)) ;;
    esac
  done
  ((i < n)) || continue
  sub=${w[i]}
  args=("${w[@]:i+1}")
  [[ "$sub" =~ ^(commit|merge|cherry-pick|revert|rebase|am|pull|push|switch|checkout)$ ]] || continue

  if [[ -n "$switched" ]]; then
    branch=$switched
  else
    branch=$(git -C "${cwd:-.}" "${repo_opts[@]}" branch --show-current 2>/dev/null) ||
      branch=$(git -C "${cwd:-.}" branch --show-current 2>/dev/null)
  fi
  on_protected=false
  is_protected "$branch" && on_protected=true

  # Positional arguments; options that take a separate value are skipped with it.
  pos=()
  for ((j = 0; j < ${#args[@]}; j++)); do
    a=${args[j]}
    case "$a" in
      -o | --push-option | --repo | --receive-pack | --exec) ((j++)) ;;
      -*) ;;
      *) pos+=("$a") ;;
    esac
  done

  case "$sub" in
    commit | merge | cherry-pick | revert | rebase | am)
      # --abort and --quit only undo an operation in progress.
      [[ " ${args[*]} " =~ \ --(abort|quit)\  ]] && continue
      $on_protected && deny "Running 'git $sub' on '$branch' is not allowed. Create a branch first (see the Git workflow section in CLAUDE.md)."
      ;;

    pull)
      $on_protected || continue
      [[ " ${args[*]} " == *" --no-ff "* ]] &&
        deny "'git pull --no-ff' on '$branch' creates a merge commit. Use a plain or --ff-only pull."
      for ref in "${pos[@]:1}"; do
        is_protected "${ref#refs/heads/}" ||
          deny "Pulling '$ref' into '$branch' merges another branch. Open a pull request instead."
      done
      ;;

    push)
      [[ " ${args[*]} " =~ \ --(all|mirror|branches)\  ]] &&
        deny "Pushing all branches would update main/master. Push a feature branch and open a pull request."
      [[ " ${args[*]} " =~ \ --(tags|follow-tags)\  ]] &&
        ask_reason="This push may include v* tags, which deploy to production."
      refspecs=("${pos[@]:1}")
      # No refspec means "push the current branch".
      if ((${#refspecs[@]} == 0)) && $on_protected; then
        deny "Pushing from '$branch' is not allowed. Create a feature branch and push that instead."
      fi
      tag_next=false
      for ref in "${refspecs[@]}"; do
        if $tag_next || [[ "$ref" == tag ]]; then
          $tag_next && [[ "$ref" == v* ]] &&
            ask_reason="Pushing tag '$ref' deploys it to production."
          [[ "$ref" == tag ]] && tag_next=true || tag_next=false
          continue
        fi
        ref=${ref#+}
        dst=${ref#*:}  # the remote side; without a colon it is the source itself
        [[ "$dst" == HEAD || "$dst" == @ ]] && dst=$branch
        dst=${dst#refs/heads/}
        is_protected "$dst" &&
          deny "Pushing to '$dst' is not allowed. Push a feature branch and open a pull request."
        [[ "$dst" == v[0-9]* || "$dst" == refs/tags/v* ]] &&
          ask_reason="Pushing tag '${dst#refs/tags/}' deploys it to production."
      done
      ;;

    switch | checkout)
      # Track the branch so `git switch main && git merge x` is checked on main.
      [[ " ${args[*]} " == *" -- "* ]] && continue
      for ((j = 0; j < ${#args[@]}; j++)); do
        case "${args[j]}" in
          -b | -B | -c | -C | --create | --force-create | --orphan)
            switched=${args[j + 1]:-}
            continue 2
            ;;
        esac
      done
      if is_protected "${pos[0]:-}" || [[ "$sub" == switch && -n "${pos[0]:-}" ]]; then
        switched=${pos[0]}
      fi
      ;;
  esac
done < <(sed -E 's/(&&|\|\||;|\||&|\(|\)|`)/\n/g' <<<"$cmd")

[[ -n "$ask_reason" ]] && decide ask "$ask_reason"
exit 0
