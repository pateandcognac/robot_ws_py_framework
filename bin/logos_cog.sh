#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: logos_startup.sh WORKSPACE_NAME [roslaunch args...]

Create or checkpoint ~/robot_workspaces/WORKSPACE_NAME, then launch:
  roslaunch logos_framework start_framework.launch workspace:=WORKSPACE_NAME

Environment:
  LOGOS_TEMPLATE_WORKSPACE  Template directory to copy for new workspaces.
                            Default: ~/robot_workspaces/Logos
  LOGOS_COG_NICE            Niceness for the framework launch process.
                            Default: -5 (requires scheduler permission).
                            Set to 0 to use normal priority.

To push back to `Logos/`, run:
$ git checkout -b <branch>
$ git push origin <branch>

USAGE
}

die() {
    printf 'logos_startup: %s\n' "$*" >&2
    exit 1
}

workspace_name="${1:-}"
if [[ -z "$workspace_name" || "$workspace_name" == "-h" || "$workspace_name" == "--help" ]]; then
    usage
    [[ -n "$workspace_name" ]] && exit 0
    exit 2
fi
shift

cog_nice_level="${LOGOS_COG_NICE:--5}"
if ! [[ "$cog_nice_level" =~ ^-?[0-9]+$ ]] || (( cog_nice_level < -20 || cog_nice_level > 19 )); then
    die "LOGOS_COG_NICE must be an integer between -20 and 19 (got $cog_nice_level)"
fi

# Verify this before checkpointing the workspace. Negative niceness is denied
# unless the account has a suitable systemd/PAM scheduler-priority grant.
if (( cog_nice_level < 0 )) && ! nice -n "$cog_nice_level" true 2>/dev/null; then
    die "cannot apply niceness $cog_nice_level; configure scheduler permission or use LOGOS_COG_NICE=0"
fi

case "$workspace_name" in
    */*|.*|*..*)
        die "workspace name must be a single directory name under ~/robot_workspaces"
        ;;
esac

robot_workspaces="${HOME}/robot_workspaces"
template_workspace="${LOGOS_TEMPLATE_WORKSPACE:-${robot_workspaces}/Logos}"
target_workspace="${robot_workspaces}/${workspace_name}"

timestamp="$(date '+%Y-%m-%d %H:%M:%S %z')"

clone_template() {
    [[ -d "$template_workspace" ]] || die "template workspace not found: $template_workspace"
    git clone "$template_workspace" "$target_workspace"
}

ensure_git_repo() {
    if [[ ! -d "${target_workspace}/.git" ]]; then
        git -C "$target_workspace" init
    fi
}

ensure_git_identity() {
    if ! git -C "$target_workspace" config user.name >/dev/null; then
        git -C "$target_workspace" config user.name "Logos Startup"
    fi

    if ! git -C "$target_workspace" config user.email >/dev/null; then
        git -C "$target_workspace" config user.email "logos-startup@localhost"
    fi
}

commit_if_needed() {
    git -C "$target_workspace" add -A

    if git -C "$target_workspace" diff --cached --quiet && git -C "$target_workspace" diff --quiet; then
        printf 'logos_startup: workspace is already clean: %s\n' "$target_workspace"
        return
    fi

    git -C "$target_workspace" commit -m "Checkpoint before Logos startup: ${timestamp}"
}

mkdir -p "$robot_workspaces"

if [[ -e "$target_workspace" && ! -d "$target_workspace" ]]; then
    die "target exists but is not a directory: $target_workspace"
fi

if [[ ! -d "$target_workspace" ]]; then
    printf 'logos_startup: cloning %s from %s\n' "$target_workspace" "$template_workspace"
    clone_template
else
    printf 'logos_startup: preparing existing workspace %s\n' "$target_workspace"
    ensure_git_repo
    ensure_git_identity
    commit_if_needed
fi

exec nice -n "$cog_nice_level" \
    roslaunch logos_framework start_framework.launch "workspace:=${workspace_name}" "$@"
