#!/usr/bin/env bash
set -euo pipefail

# Build and install RHVoice from source on Debian 11+ arm64/aarch64.
# Usage examples:
#   ./scripts/install_rhvoice_linux_aarch64.sh
#   ./scripts/install_rhvoice_linux_aarch64.sh --version 1.18.4 --voice anna

RHVOICE_VERSION=""
VOICE_TO_INSTALL=""
PREFIX="/usr"
SRC_DIR="${HOME}/src/RHVoice"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      RHVOICE_VERSION="${2:-}"
      shift 2
      ;;
    --voice)
      VOICE_TO_INSTALL="${2:-}"
      shift 2
      ;;
    --prefix)
      PREFIX="${2:-}"
      shift 2
      ;;
    --src-dir)
      SRC_DIR="${2:-}"
      shift 2
      ;;
    -h|--help)
      cat <<'EOF'
Build RHVoice from source (Debian arm64/aarch64).

Options:
  --version <tag>    Build a specific RHVoice tag (default: current master).
  --voice <name>     Install voice via rhvoice.vm after install (optional).
  --prefix <path>    Install prefix for scons install (default: /usr).
  --src-dir <path>   Source checkout directory (default: ~/src/RHVoice).
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This script is intended for Linux only." >&2
  exit 1
fi

ARCH="$(uname -m)"
if [[ "${ARCH}" != "aarch64" && "${ARCH}" != "arm64" ]]; then
  echo "Warning: expected aarch64/arm64, got ${ARCH}."
fi

if command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  SUDO=""
fi

echo "[1/6] Installing build dependencies..."
${SUDO} apt-get update
${SUDO} apt-get install -y --no-install-recommends \
  git \
  ca-certificates \
  build-essential \
  pkg-config \
  scons \
  libpulse-dev \
  libao-dev \
  portaudio19-dev \
  libspeechd-dev \
  speech-dispatcher \
  libssl-dev \
  zlib1g-dev

echo "[2/6] Cloning RHVoice sources..."
mkdir -p "$(dirname "${SRC_DIR}")"
if [[ -d "${SRC_DIR}/.git" ]]; then
  git -C "${SRC_DIR}" fetch --tags origin
  git -C "${SRC_DIR}" pull --ff-only origin master
else
  git clone --recursive https://github.com/RHVoice/RHVoice.git "${SRC_DIR}"
fi

if [[ -n "${RHVOICE_VERSION}" ]]; then
  echo "[3/6] Checking out tag ${RHVOICE_VERSION}..."
  git -C "${SRC_DIR}" checkout "tags/${RHVOICE_VERSION}" -b "build-${RHVOICE_VERSION}" 2>/dev/null || \
    git -C "${SRC_DIR}" checkout "tags/${RHVOICE_VERSION}"
else
  echo "[3/6] Using RHVoice master branch..."
fi

git -C "${SRC_DIR}" submodule update --init --recursive

echo "[4/6] Building RHVoice..."
CORES="$(getconf _NPROCESSORS_ONLN || echo 2)"
(
  cd "${SRC_DIR}"
  scons -j "${CORES}"
)

echo "[5/6] Installing RHVoice..."
(
  cd "${SRC_DIR}"
  ${SUDO} scons install prefix="${PREFIX}"
)
${SUDO} ldconfig || true

echo "[6/6] Verifying installation..."
if command -v RHVoice-test >/dev/null 2>&1; then
  echo "test" | RHVoice-test >/dev/null
  echo "OK: RHVoice-test is available."
elif command -v rhvoice.test >/dev/null 2>&1; then
  echo "test" | rhvoice.test >/dev/null
  echo "OK: rhvoice.test is available."
else
  echo "RHVoice installed, but test binary is not in PATH." >&2
  echo "Try: find ${PREFIX} -type f | grep -E 'RHVoice-test|rhvoice\\.test'" >&2
fi

if [[ -n "${VOICE_TO_INSTALL}" ]]; then
  if command -v rhvoice.vm >/dev/null 2>&1; then
    echo "Installing voice '${VOICE_TO_INSTALL}' via rhvoice.vm..."
    ${SUDO} rhvoice.vm -i "${VOICE_TO_INSTALL}"
  else
    echo "rhvoice.vm not found. Install voices from distro packages or manually." >&2
  fi
fi

echo "Done."
