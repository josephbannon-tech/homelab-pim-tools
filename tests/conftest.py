"""Tests see every toolset; each deployment switches on its own with PIM_TOOLSETS."""

import os

os.environ.setdefault("PIM_TOOLSETS", "calendar,tasks,notes,recipes")
