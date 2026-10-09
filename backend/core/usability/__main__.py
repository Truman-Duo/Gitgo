"""Offline statistics reader; does not start a model or daemon."""
import argparse
import json
from pathlib import Path
from .collector import read_summary

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--project-root", required=True, type=Path, help="Existing external project state directory")
parser.add_argument("--days", type=int, default=30)
arguments = parser.parse_args()
print(json.dumps(read_summary(arguments.project_root, days=arguments.days), ensure_ascii=False, indent=2))
