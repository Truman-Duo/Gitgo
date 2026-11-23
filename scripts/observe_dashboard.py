"""Read/control an explicitly attached Dashboard Host; never launch a Host.

Example: python scripts/observe_dashboard.py --descriptor /private/run/attach.json observer.status
Arguments may be supplied as a UTF-8 JSON file, avoiding shell escaping and
retaining exact decision identities. Mutations require --confirm-control.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core.application import OperationError
from backend.core.observer.bridge import ObserverClient, READ_OPERATIONS, WRITE_OPERATIONS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=sorted(READ_OPERATIONS | WRITE_OPERATIONS))
    parser.add_argument("--descriptor", required=True)
    parser.add_argument("--arguments-file", type=Path)
    parser.add_argument("--confirm-control", action="store_true")
    parser.add_argument("--request-id", default="")
    options = parser.parse_args()
    try:
        arguments = json.loads(options.arguments_file.read_text("utf-8-sig")) if options.arguments_file else {}
        if not isinstance(arguments, dict):
            raise ValueError("OBSERVER_ARGUMENTS_INVALID: expected an object")
        control = options.operation in WRITE_OPERATIONS
        if control and not options.confirm_control:
            raise OperationError("OBSERVER_CONTROL_CONFIRMATION_REQUIRED", "Use control only after an explicit user instruction.")
        response = ObserverClient(options.descriptor, control=control).call(options.operation, arguments,
                     confirm=options.confirm_control, request_id=options.request_id, timeout=1900 if control else 30)
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0 if response.get("ok") else 1
    except OperationError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False))
        return 1
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "OBSERVER_ATTACH_FAILED", "message": str(exc)}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
