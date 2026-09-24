#!/bin/bash
# Install the project-wide CLAUDE.md where a Claude Code cloud session loads it (MAS-881).
#
# One source (the MAS-546 shape): the canonical copy of this hook is masterly-framework
# repo-templates/project-claude-md/project-claude-md.sh. Every Masterly repository holds a
# VERBATIM copy at .claude/hooks/project-claude-md.sh, registered as a synchronous SessionStart
# hook by .claude/settings.json from the same template, with the vendored fallback beside it at
# .claude/PROJECT-CLAUDE.md. Change it in masterly-framework and re-copy; never edit a copy in
# place.
#
# Why this exists. Every Masterly repository's CLAUDE.md opens by saying that the project-wide
# context is loaded from ../CLAUDE.md, one directory above the clone. On a developer's machine
# that path is a symlink installed once by hand and verified (masterly-framework,
# docs/conventions/project-wide-claude-md.md). A Claude Code cloud container is provisioned
# fresh, with nobody at a shell to run `ln -s`, so the path was simply absent -- and every cloud
# session ran without the hard architectural constraints, silently, because a session missing
# this file has no way to notice. This hook is the cloud analogue of that one-time install.
#
# What it does, in order:
#   1. Nothing unless CLAUDE_CODE_REMOTE is "true". It never touches a developer's machine,
#      where the link is installed and verified by hand, and it prints nothing there.
#   2. Derives every path; none is typed. The clones directory is the parent of
#      CLAUDE_PROJECT_DIR, the repository root Claude Code hands a hook, and the target is
#      <clones>/CLAUDE.md -- an ancestor of the working directory, which is what Claude Code
#      loads. If CLAUDE_PROJECT_DIR is not set, nothing is derived and nothing is installed.
#   3. Chooses the source, preferring the one that cannot drift:
#        a. <clones>/masterly-framework/PROJECT-CLAUDE.md -- the tracked original, when the
#           framework repository is cloned alongside;
#        b. otherwise <repo>/.claude/PROJECT-CLAUDE.md -- the vendored copy shipped beside
#           this hook: byte-identical to the tracked file when it was copied, and behind it
#           from the tracked file's next change until it is re-copied. masterly-framework's
#           CI proves the template equals the tracked file; nothing in a consuming repository's
#           CI can see whether its copy is current.
#   4. Links the target to the source by a relative path and reports one line on stdout,
#      which the SessionStart hook contract adds to the session's context -- so the session
#      can see which source it is reading, and a person can compare it with /context.
#
# What it never does: destroy content. The target is a place a person or a platform may have
# put a file, so:
#   * anything at the target that is not a symlink is left exactly as it is. A regular file
#     whose bytes equal the chosen source is reported as an installed plain copy; anything else
#     is reported as foreign, loudly, because sessions in that container are then loading a
#     document this repository does not govern -- and it is still not replaced, because
#     replacing it is the clobber this hook exists not to do;
#   * a symlink that already resolves to the chosen source is left as it is;
#   * a symlink that resolves anywhere else, or nowhere, is re-pointed. A symlink carries no
#     content, so re-pointing it loses nothing; and in a remote container this hook is the only
#     thing that makes one, so a link to a lesser source is this hook's own earlier choice in a
#     container whose clone set has changed since (the container is cached between sessions).
#
# Synchronous, never async. Printing {"async": true} would let the session start before the
# link exists, which is exactly the race this hook exists to close. The selftest asserts that
# nothing this hook prints can be read as that marker.
#
# --selftest builds every case above in a temporary directory and asserts each outcome, so a
# hook that has quietly stopped installing fails CI instead of passing for the wrong reason.
# It exercises this file itself, with CLAUDE_CODE_REMOTE and CLAUDE_PROJECT_DIR set per case.

set -euo pipefail

TRACKED_NAME="PROJECT-CLAUDE.md"
INSTALLED_NAME="CLAUDE.md"
FRAMEWORK_DIR="masterly-framework"
VENDORED_REL=".claude/$TRACKED_NAME"
HOOK_REL=".claude/hooks/project-claude-md.sh"
TAG="project-wide CLAUDE.md"

# The physical path of an existing file or directory. `cd && pwd -P` rather than realpath or
# readlink -f, which are not on every platform the selftest runs on.
physical() {
  if [ -d "$1" ]; then
    (cd "$1" && pwd -P)
  else
    printf '%s/%s\n' "$(cd "$(dirname "$1")" && pwd -P)" "$(basename "$1")"
  fi
}

# Where a symlink chain ends, as a physical path. Fails when it ends nowhere.
resolve() {
  local p=$1 t hops=0
  while [ -L "$p" ]; do
    t=$(readlink "$p")
    case $t in
      /*) p=$t ;;
      *) p="$(dirname "$p")/$t" ;;
    esac
    hops=$((hops + 1))
    [ "$hops" -le 40 ] || return 1
  done
  [ -e "$p" ] || return 1
  physical "$p"
}

install() {
  # 1. Remote only. Silent otherwise: a developer's session gets no line of noise.
  [ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || return 0

  # 2. Derived paths, or nothing.
  if [ -z "${CLAUDE_PROJECT_DIR:-}" ] || [ ! -d "$CLAUDE_PROJECT_DIR" ]; then
    echo "$TAG: CLAUDE_PROJECT_DIR is not set to a directory, so no path can be derived; nothing installed."
    return 0
  fi
  local project clones repo target link how source
  project=$(physical "$CLAUDE_PROJECT_DIR")
  clones=$(dirname "$project")
  repo=$(basename "$project")
  target="$clones/$INSTALLED_NAME"

  # 3. The source, in preference order.
  if [ -f "$clones/$FRAMEWORK_DIR/$TRACKED_NAME" ]; then
    link="$FRAMEWORK_DIR/$TRACKED_NAME"
    how="the tracked original in the $FRAMEWORK_DIR clone alongside; it cannot drift"
  elif [ -f "$project/$VENDORED_REL" ]; then
    link="$repo/$VENDORED_REL"
    how="the vendored copy shipped in $repo; byte-identical to $FRAMEWORK_DIR/$TRACKED_NAME when it was copied, possibly behind it since"
  else
    echo "$TAG: no source. Neither $clones/$FRAMEWORK_DIR/$TRACKED_NAME nor $project/$VENDORED_REL exists, so nothing was installed at $target and this session has no project-wide context."
    return 0
  fi
  source="$clones/$link"

  # 4. The target, without destroying anything.
  if [ -L "$target" ]; then
    local current previous
    if current=$(resolve "$target") && [ "$current" = "$(physical "$source")" ]; then
      echo "$TAG: $target -> $link is already in place ($how). Installed by $repo/$HOOK_REL."
      return 0
    fi
    previous=$(readlink "$target")
    rm -f "$target"
    ln -s "$link" "$target"
    echo "$TAG: $target -> $link, re-pointed from $previous ($how). Installed by $repo/$HOOK_REL."
    return 0
  fi
  if [ -e "$target" ]; then
    if [ -f "$target" ] && cmp -s "$target" "$source"; then
      echo "$TAG: $target is a plain file identical to $link and was left as it is ($how). Checked by $repo/$HOOK_REL."
      return 0
    fi
    local msg
    msg="$TAG: WARNING. $target exists, is not a symlink, and is not the tracked file. It was NOT replaced: sessions in this container load it instead of $link. Reported by $repo/$HOOK_REL."
    echo "$msg"
    echo "$msg" >&2
    return 0
  fi
  ln -s "$link" "$target"
  echo "$TAG: $target -> $link ($how). Installed by $repo/$HOOK_REL."
}

# --- selftest -------------------------------------------------------------------------------

selftest() {
  local self failures=0 total=0
  self=$(physical "$0")
  # Not local: the EXIT trap runs after this function has returned.
  SELFTEST_TMP=$(mktemp -d)
  trap 'rm -rf "$SELFTEST_TMP"' EXIT
  local tmp=$SELFTEST_TMP

  # A clones directory with a repository in it, holding the vendored copy, and optionally
  # the framework clone beside it. Prints nothing; the caller knows the layout.
  fixture() { # <clones> <with-framework: yes|no> <with-vendored: yes|no>
    mkdir -p "$1/some-repo/.claude/hooks"
    [ "$3" = "yes" ] && printf 'vendored copy\n' >"$1/some-repo/$VENDORED_REL"
    if [ "$2" = "yes" ]; then
      mkdir -p "$1/$FRAMEWORK_DIR"
      printf 'tracked original\n' >"$1/$FRAMEWORK_DIR/$TRACKED_NAME"
    fi
    return 0
  }

  # Run the hook as Claude Code would: this file, with the two variables set per case. The
  # environment running the selftest may itself be a cloud container with CLAUDE_CODE_REMOTE
  # exported, so the "unset" case unsets it rather than inheriting it -- the first run of this
  # selftest was in exactly such a container, and the case failed for that reason.
  run() { # <remote value | unset> <project dir>
    if [ "$1" = "unset" ]; then
      env -u CLAUDE_CODE_REMOTE CLAUDE_PROJECT_DIR="$2" bash "$self" </dev/null 2>/dev/null
    else
      CLAUDE_CODE_REMOTE=$1 CLAUDE_PROJECT_DIR=$2 bash "$self" </dev/null 2>/dev/null
    fi
  }

  check() { # <name> <exit status of the predicate>
    total=$((total + 1))
    if [ "$2" -eq 0 ]; then
      echo "ok: $1"
    else
      echo "selftest FAIL: $1"
      failures=$((failures + 1))
    fi
  }

  # The predicates, named so each case reads as the claim it makes.
  links_to() { [ -L "$1" ] && [ "$(readlink "$1")" = "$2" ]; }   # <path> <link text>
  reads() { [ "$(cat "$1")" = "$2" ]; }                            # <path> <content>
  plain_file() { [ ! -L "$1" ] && [ -f "$1" ]; }                   # <path>
  absent() { [ ! -e "$1" ] && [ ! -L "$1" ]; }                     # <path>
  says() { case $1 in *"$2"*) return 0 ;; esac; return 1; }        # <output> <fragment>
  silent() { [ -z "$1" ]; }                                        # <output>
  not_json() { case $1 in "{"*) return 1 ;; esac; return 0; }      # <output>

  local c out t

  # Remote, framework alongside: the link points at the tracked original.
  c="$tmp/sibling"; t="$c/$INSTALLED_NAME"
  fixture "$c" yes yes
  out=$(run true "$c/some-repo")
  check "remote with the framework alongside links to the tracked original" \
    "$(links_to "$t" "$FRAMEWORK_DIR/$TRACKED_NAME" && reads "$t" "tracked original"; echo $?)"
  check "  ...and says so on stdout" "$(says "$out" "-> $FRAMEWORK_DIR/$TRACKED_NAME"; echo $?)"
  check "  ...and prints nothing that reads as an async marker" "$(not_json "$out"; echo $?)"

  # Running again changes nothing and says it is already in place.
  out=$(run true "$c/some-repo")
  check "a second run leaves a correct link alone" \
    "$(links_to "$t" "$FRAMEWORK_DIR/$TRACKED_NAME" && says "$out" "already in place"; echo $?)"

  # Remote, no framework: the link points at the vendored copy inside the repository.
  c="$tmp/vendored"; t="$c/$INSTALLED_NAME"
  fixture "$c" no yes
  out=$(run true "$c/some-repo")
  check "remote without the framework links to the vendored copy" \
    "$(links_to "$t" "some-repo/$VENDORED_REL" && reads "$t" "vendored copy"; echo $?)"
  check "  ...and says the copy may be behind" "$(says "$out" "possibly behind"; echo $?)"

  # The framework appears later (a cached container whose clone set changed): re-pointed.
  mkdir -p "$c/$FRAMEWORK_DIR"
  printf 'tracked original\n' >"$c/$FRAMEWORK_DIR/$TRACKED_NAME"
  out=$(run true "$c/some-repo")
  check "a link to the vendored copy is re-pointed once the framework is alongside" \
    "$(links_to "$t" "$FRAMEWORK_DIR/$TRACKED_NAME" && says "$out" "re-pointed from some-repo/$VENDORED_REL"; echo $?)"

  # A dangling link is re-pointed.
  c="$tmp/dangling"; t="$c/$INSTALLED_NAME"
  fixture "$c" yes yes
  ln -s "no-such-file.md" "$t"
  run true "$c/some-repo" >/dev/null
  check "a dangling link is re-pointed" "$(links_to "$t" "$FRAMEWORK_DIR/$TRACKED_NAME"; echo $?)"

  # Not remote: nothing happens and nothing is printed, whether unset or false.
  c="$tmp/local-unset"; t="$c/$INSTALLED_NAME"
  fixture "$c" yes yes
  out=$(run unset "$c/some-repo")
  check "not remote (variable unset) installs nothing and prints nothing" \
    "$(absent "$t" && silent "$out"; echo $?)"
  c="$tmp/local-false"; t="$c/$INSTALLED_NAME"
  fixture "$c" yes yes
  out=$(run false "$c/some-repo")
  check "not remote (variable false) installs nothing and prints nothing" \
    "$(absent "$t" && silent "$out"; echo $?)"

  # A regular file at the target is never replaced -- whatever it holds.
  c="$tmp/foreign"; t="$c/$INSTALLED_NAME"
  fixture "$c" yes yes
  printf 'somebody else put this here\n' >"$t"
  out=$(run true "$c/some-repo")
  check "a foreign regular file at the target is not replaced" \
    "$(plain_file "$t" && reads "$t" "somebody else put this here"; echo $?)"
  check "  ...and is reported as not replaced" "$(says "$out" "NOT replaced"; echo $?)"
  c="$tmp/plain-copy"; t="$c/$INSTALLED_NAME"
  fixture "$c" yes yes
  printf 'tracked original\n' >"$t"
  out=$(run true "$c/some-repo")
  check "an identical plain copy at the target is left as a plain file" \
    "$(plain_file "$t" && says "$out" "plain file identical"; echo $?)"

  # No source at all: nothing is created, and the session is told it has no context.
  c="$tmp/no-source"; t="$c/$INSTALLED_NAME"
  fixture "$c" no no
  out=$(run true "$c/some-repo")
  check "no source anywhere installs nothing and says so" \
    "$(absent "$t" && says "$out" "no source"; echo $?)"

  # No project directory: nothing is derived.
  c="$tmp/no-project"; t="$c/$INSTALLED_NAME"
  fixture "$c" yes yes
  out=$(CLAUDE_CODE_REMOTE=true bash "$self" </dev/null 2>/dev/null)
  check "an unset CLAUDE_PROJECT_DIR derives nothing" \
    "$(absent "$t" && says "$out" "CLAUDE_PROJECT_DIR is not set"; echo $?)"

  # Synchronous by construction: no line of code in this file carries the async marker. The
  # pattern is a key followed by a colon, which this line does not contain.
  check "the hook never emits an async marker" \
    "$(! grep -v '^ *#' "$self" | grep -qE '"async"[[:space:]]*:'; echo $?)"

  if [ "$failures" -ne 0 ]; then
    echo
    echo "FAIL: $failures of $total selftest case(s) failed."
    return 1
  fi
  echo "OK: $total selftest cases pass."
}

if [ "${1:-}" = "--selftest" ]; then
  selftest
else
  install
fi
