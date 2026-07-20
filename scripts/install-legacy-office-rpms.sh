#!/usr/bin/env bash
# Install bundled LibreOffice RPMs for legacy .doc/.ppt/.xls preconversion.
#
# Idempotent: exits 0 when soffice + prlimit are already usable. Requires root
# (sudo) on the docling-serve host. Uses only the offline RPM tree shipped with
# this repo — no outbound package mirrors at install time.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RPM_DIR="${ROOT}/offline-packages/rpms"

if command -v soffice >/dev/null 2>&1 && command -v prlimit >/dev/null 2>&1; then
  echo "LibreOffice already installed: $(readlink -f "$(command -v soffice)")"
  "$(readlink -f "$(command -v soffice)")" --headless --version
  exit 0
fi

if [[ ! -d "${RPM_DIR}" ]] || ! compgen -G "${RPM_DIR}/*.rpm" >/dev/null; then
  echo "Missing offline RPM bundle at ${RPM_DIR}." >&2
  echo "Populate it with scripts/download-legacy-office-rpms.sh on a connected host." >&2
  exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Re-running with sudo ..."
  exec sudo -E bash "$0" "$@"
fi

if [[ -f "${ROOT}/offline-packages/rpms.sha256" ]]; then
  echo "Verifying RPM checksums ..."
  (cd "${RPM_DIR}" && sha256sum -c ../rpms.sha256)
fi

echo "Installing ${RPM_DIR}/*.rpm ..."
# Local RPM install only — no repository metadata fetch.
dnf install -y --disablerepo='*' "${RPM_DIR}"/*.rpm

test -x "$(readlink -f "$(command -v soffice)")"
prlimit --version >/dev/null
"$(readlink -f "$(command -v soffice)")" --headless --version
echo "Legacy Office runtime is ready."
