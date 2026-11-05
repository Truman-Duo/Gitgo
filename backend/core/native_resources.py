"""Native resource names shared by launch and recovery; no product workflow."""
import os
from pathlib import Path
import re
import sys


def windows_job_name(recovery_id):
    if not isinstance(recovery_id, str):
        raise ValueError("Invalid native recovery identity")
    if not recovery_id:
        return None
    if not re.fullmatch(r"[0-9a-f]{32}", recovery_id):
        raise ValueError("Invalid native recovery identity")
    # Stable wire namespace: existing recovery records and running jobs use it.
    # Its historical spelling does not make Publish the owner of this API.
    return "Local\\Gitgo.Publish." + recovery_id


def recovery_location(recovery_id):
    name = windows_job_name(recovery_id)
    if sys.platform == "win32":
        return {"platform": "win32", "name": name}
    if sys.platform == "linux":
        root = os.environ.get("GITGO_SANDBOX_CGROUP_ROOT", "")
        return {"platform": "linux", "root": str(Path(root).resolve()) if root else ""}
    return {"platform": sys.platform}
