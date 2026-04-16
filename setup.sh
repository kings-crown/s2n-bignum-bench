#!/bin/bash
set -euo pipefail

reuse_existing=false

while [[ "$#" -gt 0 && "$1" == --* ]]; do
  case "$1" in
    --reuse)
      reuse_existing=true
      shift
      ;;
    *)
      echo "Unknown option: $1"
      echo "setup.sh [--reuse] <arch(arm or x86)> <number of cores>"
      exit 1
      ;;
  esac
done

if [ "$#" -ne 2 ]; then
  echo "setup.sh [--reuse] <arch(arm or x86)> <number of cores>"
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

### Setup HOL Light


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
git checkout e8944de23c0882b83bbbcd828ed1cf56d3a62b90


make switch-5
eval $(opam env --set-switch)
export HOLLIGHT_USE_MODULE=1
make
export HOLLIGHT_DIR=`pwd`

echo "Building TacticTrace of HOL Light..."

cd TacticTrace
make

export TACLOGGER_DIR=`pwd`
./build-hol-kernel.sh
cd ../..

### Setup s2n-bignum and collect top-level theorems

echo "Building object files of s2n-bignum..."

if [ "$reuse_existing" != "true" ]; then
  rm -rf s2n-bignum
elif [ -d s2n-bignum ]; then
  echo "Reusing existing s2n-bignum checkout"
fi

if [ ! -d s2n-bignum ]; then
  git clone https://github.com/kings-crown/s2n-bignum.git
fi
cd s2n-bignum
git checkout c7f0988c7660bc3d182cbc4380507b620c2a82f1


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
