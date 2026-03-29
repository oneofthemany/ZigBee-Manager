#!/bin/bash
set -e

ARCH=$(uname -m)
case "$ARCH" in
  aarch64) TARGET="aarch64-unknown-linux-gnu" ;;
  x86_64)  TARGET="x86_64-unknown-linux-gnu"  ;;
  *) echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

# Python 3.14 may be newer than pyo3's max supported version.
# ABI3 forward compat allows building against the stable ABI anyway.
export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1

cd zmm_cpc
maturin build --release --target "$TARGET"
pip install --force-reinstall target/wheels/*.whl --break-system-packages