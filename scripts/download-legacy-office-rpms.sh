#!/usr/bin/env bash
# Populate offline-packages/rpms for air-gapped / ATO installs.
#
# Run on a connected build host (or GitLab CI) that can pull the CentOS Stream 9
# base image. The resulting RPM tree is committed or published as a GitLab
# package artifact; target hosts install with install-legacy-office-rpms.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${ROOT}/offline-packages/rpms"
IMAGE="${LEGACY_OFFICE_RPM_IMAGE:-quay.io/centos/centos:stream9}"

mkdir -p "${OUT}"

packages=(
  libreoffice-core
  libreoffice-writer
  libreoffice-calc
  libreoffice-impress
  util-linux
)

echo "Downloading legacy Office RPM closure into ${OUT} ..."
docker run --rm -v "${OUT}:/out" "${IMAGE}" bash -lc "
set -euo pipefail
dnf install -y dnf-plugins-core
dnf config-manager --enable crb
dnf install -y --downloadonly --downloaddir=/out ${packages[*]}
"

count="$(find "${OUT}" -maxdepth 1 -name '*.rpm' | wc -l | tr -d ' ')"
if [[ "${count}" -lt 10 ]]; then
  echo "Expected a non-trivial RPM bundle; found ${count}." >&2
  exit 1
fi

( cd "${OUT}" && sha256sum ./*.rpm > ../rpms.sha256 )
echo "Wrote ${count} RPM(s) and offline-packages/rpms.sha256"
