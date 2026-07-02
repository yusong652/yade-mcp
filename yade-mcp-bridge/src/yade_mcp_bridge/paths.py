# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""On-disk paths for the bridge's working directory."""

import os

# Bridge working directory.
DATA_DIR = ".yade-mcp"

# Per-run and per-task logs.
LOGS_DIR = os.path.join(DATA_DIR, "logs")
