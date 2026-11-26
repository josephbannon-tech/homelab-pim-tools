"""Tool-calling eval set for chat front ends that sit on pim-tools."""

import os

# The eval server is one instance with every toolset on, so one run covers them all.
os.environ.setdefault("PIM_TOOLSETS", "calendar,tasks,notes,recipes")
