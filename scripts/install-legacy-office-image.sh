#!/usr/bin/env bash
# Install the legacy Office sidecar for air-gapped docling-serve hosts.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${CAPTIFY_LEGACY_OFFICE_IMAGE:-captify-legacy-office:el9}"
ARCHIVE="${ROOT}/offline-packages/captify-legacy-office-el9.tar.gz"
ARCHIVE_CHECKSUM="${ARCHIVE}.sha256"
INSTALL_ROOT="/opt/libreoffice"

if [[ ! -f "${ARCHIVE}" ]]; then
  echo "Missing ${ARCHIVE}. Build on a connected host with:" >&2
  echo "  bash scripts/build-legacy-office-image.sh --export" >&2
  exit 1
fi
if [[ ! -f "${ARCHIVE_CHECKSUM}" ]]; then
  echo "Missing required archive checksum: ${ARCHIVE_CHECKSUM}" >&2
  exit 1
fi
(
  cd "$(dirname "${ARCHIVE}")"
  sha256sum --check "$(basename "${ARCHIVE_CHECKSUM}")"
)

if docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  echo "Legacy Office image already loaded: ${IMAGE}"
else
  echo "Loading ${ARCHIVE} ..."
  gunzip -c "${ARCHIVE}" | docker load
fi

if [[ "$(id -u)" -ne 0 ]]; then
  sudo mkdir -p "${INSTALL_ROOT}/bin"
  sudo install -m 755 "${ROOT}/bin/soffice-wrapper.sh" "${INSTALL_ROOT}/bin/soffice-wrapper.sh"
else
  mkdir -p "${INSTALL_ROOT}/bin"
  install -m 755 "${ROOT}/bin/soffice-wrapper.sh" "${INSTALL_ROOT}/bin/soffice-wrapper.sh"
fi

docker run --rm "${IMAGE}" soffice --headless --version
echo "Legacy Office sidecar ready. Point docling-serve at ${INSTALL_ROOT}/bin/soffice-wrapper.sh"
