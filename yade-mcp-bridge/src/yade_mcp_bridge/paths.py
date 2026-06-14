# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Canonical on-disk paths for the bridge's working directory.

All bridge artifacts — run/task logs, task records, console history — live
under a single dot-directory in the YADE process CWD. The directory name is
defined once here so it has a single home instead of being repeated across
modules.
"""

import os

# Bridge working directory, created in the YADE process CWD.
DATA_DIR = ".yade-mcp"

# Per-run and per-task logs.
LOGS_DIR = os.path.join(DATA_DIR, "logs")
