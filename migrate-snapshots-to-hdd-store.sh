#!/usr/bin/env bash
# Move only native GNS3 snapshot directories to a mounted HDD, leaving active
# projects, node disks, and controller configuration on the SSD.
set -euo pipefail

PROJECTS_DIR="${1:-/home/gns3/GNS3/projects}"
SNAPSHOT_ROOT="${2:-/mnt/HDD_STORE/gns3-snapshots}"

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo." >&2
  exit 1
fi
if [[ ! -d "${PROJECTS_DIR}" ]]; then
  echo "Projects directory does not exist: ${PROJECTS_DIR}" >&2
  exit 1
fi
mount_probe="${SNAPSHOT_ROOT}"
while [[ ! -e "${mount_probe}" && "${mount_probe}" != "/" ]]; do
  mount_probe="$(dirname "${mount_probe}")"
done
storage_mount="$(findmnt -n -o TARGET -T "${mount_probe}" 2>/dev/null || true)"
if [[ -z "${storage_mount}" || "${storage_mount}" == "/" ]] || ! mountpoint -q "${storage_mount}"; then
  echo "${SNAPSHOT_ROOT} is not on a separate mounted filesystem; refusing to move snapshots." >&2
  exit 1
fi

install -d -o gns3 -g gns3 -m 0750 "${SNAPSHOT_ROOT}"

while IFS= read -r -d '' project_dir; do
  project_id="$(basename "${project_dir}")"
  snapshots_dir="${project_dir}/snapshots"
  target_dir="${SNAPSHOT_ROOT}/${project_id}"

  if [[ -L "${snapshots_dir}" ]]; then
    echo "Already linked: ${project_id}"
    continue
  fi
  if [[ -e "${target_dir}" ]]; then
    echo "Refusing ${project_id}: destination already exists: ${target_dir}" >&2
    exit 1
  fi

  if [[ -d "${snapshots_dir}" ]]; then
    mv "${snapshots_dir}" "${target_dir}"
  else
    install -d -o gns3 -g gns3 -m 0750 "${target_dir}"
  fi
  chown -R gns3:gns3 "${target_dir}"
  ln -s "${target_dir}" "${snapshots_dir}"
  echo "Moved snapshots for ${project_id}"
done < <(find "${PROJECTS_DIR}" -mindepth 1 -maxdepth 1 -type d -print0)

echo "Done. Active labs remain in ${PROJECTS_DIR}; native snapshots are under ${SNAPSHOT_ROOT}."
