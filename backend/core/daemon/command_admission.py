"""Transport-neutral command identity, distinct from response correlation."""
import hashlib
import json

# Read-only queries remain fresh. They never acquire an execution claim.
MUTATING_COMMANDS = frozenset({
    "task", "fork_agent", "dispatch_tool", "scan", "formalize", "sync", "push",
    "trial", "round_complete", "reject", "llm_call",
})


def fingerprint(command):
    body = {key: value for key, value in command.items() if key not in {"request_id", "command_id"}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def admit(storage, command):
    key = str(command.get("command_id") or "")
    if storage is None or not key or command.get("cmd") not in MUTATING_COMMANDS:
        return None
    digest = fingerprint(command)
    claim = storage.claim_command(key, digest, process_id=str(command.get("process_id") or ""))
    return key, digest, claim
