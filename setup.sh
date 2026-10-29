#!/bin/bash
set -euo pipefail

reuse_existing=false
skip_hol=false

while [[ "$#" -gt 0 && "$1" == --* ]]; do
  case "$1" in
    --reuse)
      reuse_existing=true
      shift
      ;;
    --skip-hol)
      skip_hol=true
      shift
      ;;
    *)
      echo "Unknown option: $1"
      echo "setup.sh [--reuse] [--skip-hol] <arch(arm or x86)> <number of cores>"
      exit 1
      ;;
  esac
done

if [ "$#" -ne 2 ]; then
  echo "setup.sh [--reuse] [--skip-hol] <arch(arm or x86)> <number of cores>"
  exit 1
fi

arch="$1"
NUM_CORES="$2"

if [ "$arch" != "arm" ] && [ "$arch" != "x86" ]; then
  echo "Arch must be either 'arm' or 'x86'"
  exit 1
fi

if ! [[ "$NUM_CORES" =~ ^[0-9]+$ ]] || [ "$NUM_CORES" -le 0 ]; then
  echo "Number of cores must be a positive integer"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"
OBJFILES_DIR="${SCRIPT_DIR}/objfiles/${arch}"
TACTICTRACE_COMMIT=7ea3808cc28258113fd41ef9d860dbfe7691c190
S2N_BIGNUM_REPO=https://github.com/kings-crown/s2n-bignum.git
S2N_BIGNUM_COMMIT=5e0fc782ae6d587c2ae40faf2c2c4d0b637e6240

### Setup HOL Light


if [ "$skip_hol" = "true" ]; then

  echo "Skipping HOL Light build; reusing ${SCRIPT_DIR}/hol-light"

  if [ ! -d hol-light ]; then
    echo "--skip-hol was given but ${SCRIPT_DIR}/hol-light does not exist" >&2
    exit 1
  fi
  cd hol-light

  # The opam switch is directory-local and registered under the checkout's real
  # path, so resolve symlinks before selecting it.
  eval $(opam env --switch="$(pwd -P)" --set-switch)
  export HOLLIGHT_USE_MODULE=1
  export HOLLIGHT_DIR=`pwd`

  # build-proof.sh (after s2n-bignum.patch) shells out to TacticTrace, so a
  # skipped build still has to point TACLOGGER_DIR at an already-built tree.
  export TACLOGGER_DIR="${HOLLIGHT_DIR}/TacticTrace"
  if [ ! -x "${TACLOGGER_DIR}/tracer" ] ||
     [ ! -s "${TACLOGGER_DIR}/kernel_wrapper.ml" ] ||
     [ ! -s "${TACLOGGER_DIR}/hol_lib_inlined.mli" ]; then
    echo "TacticTrace is not fully built in ${TACLOGGER_DIR}; rerun with --reuse and without --skip-hol" >&2
    exit 1
  fi
  cd ..

else

  echo "Building HOL Light..."

  if [ "$reuse_existing" != "true" ]; then
    rm -rf hol-light
  elif [ -d hol-light ]; then
    echo "Reusing existing hol-light checkout"
  fi

  if [ ! -d hol-light ]; then
    git clone https://github.com/jrh13/hol-light.git
  fi
  cd hol-light
  git checkout d30e8239887b2348a063f82232a864de1cc2df15


  make switch-5
  eval $(opam env --set-switch)
  export HOLLIGHT_USE_MODULE=1
  make
  export HOLLIGHT_DIR=`pwd`

  echo "Building TacticTrace..."

  # TacticTrace is now a separate repository, but its scripts still require
  # it to live at ${HOLLIGHT_DIR}/TacticTrace.
  if [ -d TacticTrace ] && [ ! -e TacticTrace/.git ]; then
    # --reuse may leave generated files from the former bundled TacticTrace.
    # Preserve them before cloning the standalone repository.
    tactictrace_backup="$(mktemp -d "${HOLLIGHT_DIR}/TacticTrace.bundled.XXXXXX")"
    mv TacticTrace "${tactictrace_backup}/TacticTrace"
    echo "Saved bundled TacticTrace files in ${tactictrace_backup}/TacticTrace"
  fi
  if [ ! -d TacticTrace ]; then
    git clone https://github.com/hol-light-family/TacticTrace.git TacticTrace
  fi
  cd TacticTrace
  if ! git cat-file -e "${TACTICTRACE_COMMIT}^{commit}" 2>/dev/null; then
    git fetch origin "$TACTICTRACE_COMMIT"
  fi
  git checkout "$TACTICTRACE_COMMIT"

  export TACLOGGER_DIR=`pwd`
  # This script cleans and rebuilds the tracer before generating the wrappers.
  ./build-hol-kernel.sh
  cd ../..

fi

### Setup s2n-bignum and collect top-level theorems

echo "Building object files of s2n-bignum..."

if [ "$reuse_existing" != "true" ]; then
  rm -rf s2n-bignum
elif [ -d s2n-bignum ]; then
  echo "Reusing existing s2n-bignum checkout"
fi

if [ ! -d s2n-bignum ]; then
  git clone "$S2N_BIGNUM_REPO" s2n-bignum
fi
cd s2n-bignum
# Reused checkouts may not contain the pinned revision.
if ! git cat-file -e "${S2N_BIGNUM_COMMIT}^{commit}" 2>/dev/null; then
  git fetch "$S2N_BIGNUM_REPO" "$S2N_BIGNUM_COMMIT"
fi
git checkout "$S2N_BIGNUM_COMMIT"


# A. Prepare object files
cd "$arch"

mkdir -p "$(dirname "$OBJFILES_DIR")"
rm -rf "$OBJFILES_DIR"
mkdir -p "$OBJFILES_DIR"

make -j${NUM_CORES}

# Not all .S files are in the default location the unoptimized files are under */unopt/ (e.g. fastmul/p256/p384/p521).
if [ "$arch" = "arm" ]; then
  make unopt -j${NUM_CORES}
fi

# Copy all produced objects (including unopt) into objfiles tree
find . -name "*.o" -exec cp --parents {} "$OBJFILES_DIR" \;
if [ "$arch" == "x86" ]; then
  make winobj -j${NUM_CORES}
  find . -name "*.obj" -exec cp --parents {} "$OBJFILES_DIR" \;
fi
cd ..

# B. Collect the top-level theorems

echo "Collecting the top-level theorems..."

if git apply --reverse --check ../s2n-bignum.patch >/dev/null 2>&1; then
  echo "s2n-bignum.patch already applied, skipping"
else
  git apply ../s2n-bignum.patch || {
    echo "Failed to apply s2n-bignum.patch; please clean repository state (e.g., git status)" >&2
    exit 1
  }
fi
cd $arch # or x86


export HOLLIGHT_DIR="${SCRIPT_DIR}/hol-light"
export TOPLEVEL_THMS_DIR=$HOLLIGHT_DIR/../toplevel-thms/$arch
export HOLDIR=$HOLLIGHT_DIR
mkdir -p "$TOPLEVEL_THMS_DIR"


make build_proofs -j${NUM_CORES}
rm -rf trace-logs
