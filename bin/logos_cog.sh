#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: logos_cog.sh [options] WORKSPACE_NAME [roslaunch args...]

Create or checkpoint ~/robot_workspaces/WORKSPACE_NAME, then launch:
  roslaunch logos_framework start_framework.launch workspace:=WORKSPACE_NAME

Options:
  --sync-from-logos       Before launch, merge committed Logos/ changes into
                          an existing workspace after checkpointing it.
  --sync-only             Perform the same guarded sync, then exit.
  --template-branch NAME  Canonical Logos/ branch to clone or sync.
                          Default: master
  -h, --help              Show this help.

Environment:
  LOGOS_WORKSPACES_ROOT     Parent directory for Logos workspaces.
                            Default: ~/robot_workspaces
  LOGOS_TEMPLATE_WORKSPACE  Template directory to copy for new workspaces.
                            Default: ~/robot_workspaces/Logos
  LOGOS_TEMPLATE_BRANCH     Canonical branch to clone or sync.
                            Default: master

Examples:
  logos_cog.sh Logos_001
  logos_cog.sh --sync-from-logos Logos_001
  logos_cog.sh --sync-only Logos_001

Sync only transfers committed Git content. Runtime state/ and ipc/ remain local
and must stay untracked. To offer good clone changes back to Logos/, commit them
on a feature branch and run the clone's tools/push_to_logos.sh helper.

USAGE
}

die() {
    printf 'logos_cog: %s\n' "$*" >&2
    exit 1
}

sync_from_template=0
sync_only=0
template_branch="${LOGOS_TEMPLATE_BRANCH:-master}"
workspace_name=""

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --sync-from-logos)
            sync_from_template=1
            shift
            ;;
        --sync-only)
            sync_from_template=1
            sync_only=1
            shift
            ;;
        --template-branch)
            [[ "$#" -ge 2 ]] || die "--template-branch requires a branch name"
            template_branch="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        -*)
            die "unknown option before workspace name: $1"
            ;;
        *)
            workspace_name="$1"
            shift
            break
            ;;
    esac
done

[[ -n "$workspace_name" ]] || {
    usage >&2
    exit 2
}

[[ -n "$template_branch" ]] || die "template branch must not be empty"

case "$workspace_name" in
    */*|.*|*..*)
        die "workspace name must be a single directory name under ~/robot_workspaces"
        ;;
esac

robot_workspaces="${LOGOS_WORKSPACES_ROOT:-${HOME}/robot_workspaces}"
template_workspace="${LOGOS_TEMPLATE_WORKSPACE:-${robot_workspaces}/Logos}"
target_workspace="${robot_workspaces}/${workspace_name}"

timestamp="$(date '+%Y-%m-%d %H:%M:%S %z')"

clone_template() {
    [[ -d "$template_workspace" ]] || die "template workspace not found: $template_workspace"
    git -C "$template_workspace" show-ref --verify --quiet "refs/heads/${template_branch}" \
        || die "template branch not found: ${template_branch}"
    git clone --branch "$template_branch" "$template_workspace" "$target_workspace"
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

    if git -C "$target_workspace" diff --cached --quiet; then
        printf 'logos_cog: workspace is already clean: %s\n' "$target_workspace"
        return
    fi

    git -C "$target_workspace" commit -m "Checkpoint before Logos startup: ${timestamp}"
}

assert_runtime_paths_untracked() {
    local repo="$1"
    local label="$2"
    local runtime_path
    local tracked_paths

    tracked_paths="$(git -C "$repo" ls-files -- state ipc)"
    if [[ -n "$tracked_paths" ]]; then
        printf 'logos_cog: refusing to sync because %s tracks runtime paths:\n' "$label" >&2
        printf '%s\n' "$tracked_paths" >&2
        die "remove state/ and ipc/ from Git tracking before syncing"
    fi

    for runtime_path in state ipc; do
        if ! git -C "$repo" check-ignore --quiet --no-index \
            "${runtime_path}/.logos-sync-probe"; then
            die "${label} does not ignore ${runtime_path}/; refusing to checkpoint or sync"
        fi
    done
}

assert_template_ready() {
    local current_branch
    local template_changes

    [[ -d "${template_workspace}/.git" ]] \
        || die "template workspace is not a Git repository: $template_workspace"

    current_branch="$(git -C "$template_workspace" branch --show-current)"
    [[ "$current_branch" == "$template_branch" ]] \
        || die "template is on branch '${current_branch:-detached}', expected '$template_branch'"

    template_changes="$(git -C "$template_workspace" status --porcelain)"
    [[ -z "$template_changes" ]] \
        || die "template has uncommitted changes; commit them before syncing"

    assert_runtime_paths_untracked "$template_workspace" "template workspace"
}

assert_template_origin() {
    local origin_url
    local expected_path
    local actual_path

    origin_url="$(git -C "$target_workspace" remote get-url origin 2>/dev/null || true)"
    [[ -n "$origin_url" ]] \
        || die "workspace has no origin remote; cannot identify its Logos/ template"

    case "$origin_url" in
        file://*)
            actual_path="$(readlink -f "${origin_url#file://}")"
            ;;
        /*)
            actual_path="$(readlink -f "$origin_url")"
            ;;
        *)
            die "workspace origin is not a local Logos/ path: $origin_url"
            ;;
    esac
    expected_path="$(readlink -f "$template_workspace")"
    [[ "$actual_path" == "$expected_path" ]] \
        || die "workspace origin '$actual_path' does not match template '$expected_path'"
}

sync_from_logos() {
    local upstream_ref="refs/remotes/origin/${template_branch}"
    local before_revision
    local after_revision

    assert_template_ready
    assert_runtime_paths_untracked "$target_workspace" "target workspace"
    assert_template_origin

    before_revision="$(git -C "$target_workspace" rev-parse --short HEAD)"
    printf 'logos_cog: fetching committed %s from %s\n' \
        "$template_branch" "$template_workspace"
    git -C "$target_workspace" fetch --no-tags origin \
        "+refs/heads/${template_branch}:${upstream_ref}"

    if git -C "$target_workspace" merge-base --is-ancestor "$upstream_ref" HEAD; then
        printf 'logos_cog: workspace already contains Logos/%s at %s\n' \
            "$template_branch" "$before_revision"
        return
    fi

    printf 'logos_cog: merging Logos/%s into workspace branch %s\n' \
        "$template_branch" "$(git -C "$target_workspace" branch --show-current)"
    if ! git -C "$target_workspace" merge --no-edit "$upstream_ref"; then
        git -C "$target_workspace" merge --abort || true
        die "sync conflicted and was aborted; checkpoint ${before_revision} is intact"
    fi

    after_revision="$(git -C "$target_workspace" rev-parse --short HEAD)"
    printf 'logos_cog: sync complete: %s -> %s (state/ and ipc/ untouched)\n' \
        "$before_revision" "$after_revision"
}

if [[ "$sync_from_template" -eq 1 && "$target_workspace" == "$template_workspace" ]]; then
    die "cannot sync the template workspace from itself"
fi

mkdir -p "$robot_workspaces"

if [[ -e "$target_workspace" && ! -d "$target_workspace" ]]; then
    die "target exists but is not a directory: $target_workspace"
fi

if [[ ! -d "$target_workspace" ]]; then
    printf 'logos_cog: cloning %s branch %s from %s\n' \
        "$target_workspace" "$template_branch" "$template_workspace"
    clone_template
else
    printf 'logos_cog: preparing existing workspace %s\n' "$target_workspace"
    ensure_git_repo
    ensure_git_identity
    if [[ "$sync_from_template" -eq 1 ]]; then
        # Preflight before checkpointing so a legacy workspace that tracks
        # runtime data cannot accidentally commit more of it.
        assert_template_ready
        assert_runtime_paths_untracked "$target_workspace" "target workspace"
        assert_template_origin
    fi
    commit_if_needed
fi

if [[ "$sync_from_template" -eq 1 && -d "$target_workspace" ]]; then
    sync_from_logos
fi

if [[ "$sync_only" -eq 1 ]]; then
    printf 'logos_cog: sync-only requested; not launching cognition\n'
    exit 0
fi

exec roslaunch logos_framework start_framework.launch "workspace:=${workspace_name}" "$@"
