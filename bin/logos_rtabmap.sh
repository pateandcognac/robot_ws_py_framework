#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_DB="${HOME}/.ros/logos_rtabmap.db"
DEFAULT_MAP_DIR="${HOME}/maps"

source_ros() {
  # shellcheck disable=SC1091
  source /opt/ros/noetic/setup.bash
  if [ -f "${HOME}/tb2_ws/devel/setup.bash" ]; then
    # shellcheck disable=SC1091
    source "${HOME}/tb2_ws/devel/setup.bash"
  fi
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/devel/setup.bash"
}

usage() {
  cat <<'EOF'
Usage: bin/logos_rtabmap.sh <command> [args]

Commands:
  map [db]              Start RTAB-Map mapping. Default db: ~/.ros/logos_rtabmap.db
  fresh [db]            Start mapping and delete the selected RTAB-Map db first.
  localize [db]         Start RTAB-Map localization-only against an existing db.
  nav [db]              Start RTAB-Map localization plus Logos move_base.
  save2d [name]         Save /map to ~/maps/<name>.yaml and .pgm.
  save2d-to <prefix>    Save /map to an explicit path prefix.
  amcl <map.yaml>       Start classic AMCL navigation with a saved 2D map.
  info [db]             Print RTAB-Map database summary.
  view [db]             Open rtabmap-databaseViewer for the db.
  export-cloud [db]     Export an assembled point cloud next to the db.

Examples:
  bin/logos_rtabmap.sh map
  bin/logos_rtabmap.sh save2d kitchen_first_pass
  bin/logos_rtabmap.sh amcl ~/maps/kitchen_first_pass.yaml
  bin/logos_rtabmap.sh localize
EOF
}

need_arg() {
  local value="${1:-}"
  local message="$2"
  if [ -z "${value}" ]; then
    echo "${message}" >&2
    exit 2
  fi
}

expand_path() {
  local path="$1"
  if [[ "${path}" == "~/"* ]]; then
    printf '%s/%s\n' "${HOME}" "${path#~/}"
  else
    printf '%s\n' "${path}"
  fi
}

command="${1:-}"
shift || true

case "${command}" in
  map)
    source_ros
    db="$(expand_path "${1:-${DEFAULT_DB}}")"
    roslaunch logos_bringup logos_rtabmap.launch database_path:="${db}"
    ;;
  fresh)
    source_ros
    db="$(expand_path "${1:-${DEFAULT_DB}}")"
    roslaunch logos_bringup logos_rtabmap.launch database_path:="${db}" delete_db_on_start:=true
    ;;
  localize)
    source_ros
    db="$(expand_path "${1:-${DEFAULT_DB}}")"
    roslaunch logos_bringup logos_rtabmap.launch database_path:="${db}" localization:=true
    ;;
  nav)
    source_ros
    db="$(expand_path "${1:-${DEFAULT_DB}}")"
    roslaunch logos_bringup logos_rtabmap.launch database_path:="${db}" localization:=true with_move_base:=true
    ;;
  save2d)
    source_ros
    name="${1:-logos_rtabmap_$(date +%Y%m%d_%H%M%S)}"
    mkdir -p "${DEFAULT_MAP_DIR}"
    rosrun map_server map_saver map:=/map -f "${DEFAULT_MAP_DIR}/${name}"
    ;;
  save2d-to)
    source_ros
    prefix="${1:-}"
    need_arg "${prefix}" "save2d-to needs an output prefix, e.g. ~/maps/kitchen"
    prefix="$(expand_path "${prefix}")"
    mkdir -p "$(dirname "${prefix}")"
    rosrun map_server map_saver map:=/map -f "${prefix}"
    ;;
  amcl)
    source_ros
    map_file="${1:-}"
    need_arg "${map_file}" "amcl needs a saved map yaml, e.g. ~/maps/kitchen.yaml"
    roslaunch logos_bringup logos_navigation.launch map_file:="$(expand_path "${map_file}")"
    ;;
  info)
    source_ros
    db="$(expand_path "${1:-${DEFAULT_DB}}")"
    rtabmap-info "${db}"
    ;;
  view)
    source_ros
    db="$(expand_path "${1:-${DEFAULT_DB}}")"
    rtabmap-databaseViewer "${db}"
    ;;
  export-cloud)
    source_ros
    db="$(expand_path "${1:-${DEFAULT_DB}}")"
    rtabmap-export --cloud --output "$(basename "${db}" .db)_cloud" "${db}"
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    echo "Unknown command: ${command}" >&2
    usage >&2
    exit 2
    ;;
esac
