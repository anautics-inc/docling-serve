#!/usr/bin/env bash
# Build and optionally export the legacy Office sidecar image for offline installs.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${CAPTIFY_LEGACY_OFFICE_IMAGE:-captify-legacy-office:el9}"
EXPORT="${ROOT}/offline-packages/captify-legacy-office-el9.tar.gz"
EXPORT_CHECKSUM="${EXPORT}.sha256"

docker build -f "${ROOT}/deploy/docker/Dockerfile.legacy-office" -t "${IMAGE}" "${ROOT}"

docker run --rm "${IMAGE}" soffice --headless --version

if [[ "${1:-}" == "--export" ]]; then
  mkdir -p "${ROOT}/offline-packages"
  docker save "${IMAGE}" | gzip -9 > "${EXPORT}"
  (
    cd "$(dirname "${EXPORT}")"
    sha256sum "$(basename "${EXPORT}")" > "$(basename "${EXPORT_CHECKSUM}")"
  )
  echo "Exported ${EXPORT}"
  echo "Wrote ${EXPORT_CHECKSUM}"
fi
