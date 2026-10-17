#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Apply an explicit Kconfig fragment without executing configuration as code."""

import argparse
from pathlib import Path
import re

SETTING = re.compile(r"(?:CONFIG_([A-Z0-9_]+)=.+|# CONFIG_([A-Z0-9_]+) is not set)")


def merge(config, fragment):
    overrides = {}
    for line in fragment.read_text().splitlines():
        match = SETTING.fullmatch(line)
        if match:
            overrides[match[1] or match[2]] = line
        elif line.strip() and not line.startswith("#"):
            raise ValueError(f"Invalid Kconfig fragment line: {line}")
    lines = []
    for line in config.read_text().splitlines():
        match = SETTING.fullmatch(line)
        if not match or (match[1] or match[2]) not in overrides:
            lines.append(line)
    config.write_text("\n".join(lines + list(overrides.values())) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("fragment", type=Path)
    args = parser.parse_args()
    merge(args.config, args.fragment)
