#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Keep the historical entry point, but execute only the checked-out local helper.
set -euo pipefail
exec python3 "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/sign_kernel.py" "$@"
