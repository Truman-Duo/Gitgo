"""Compatibility names for the same file effects; never authority grants."""
FILE_ALIASES = {"file_write": "write_file", "file_edit": "edit_file", "file_delete": "delete_file"}
FILE_MUTATION_TOOLS = frozenset({"write_file", "edit_file", "delete_file", "apply_patch", "document_create", "promote_agent_changes", "exec_command", "shell_script", *FILE_ALIASES})


def canonical_file_operation(name):
    return FILE_ALIASES.get(name, name)


def checker_covers(operation, tools):
    return canonical_file_operation(operation) in {canonical_file_operation(name) for name in tools}


PROTECTED_METADATA_PARTS = frozenset({".git", ".gitgo"})


def protected_metadata_part(value):
    import os
    name = str(value)
    if os.name == "nt":
        # Win32 ignores trailing spaces/dots and interprets ':' as a stream.
        name = name.split(":", 1)[0].rstrip(" .")
    return name.casefold() in PROTECTED_METADATA_PARTS


def metadata_mutation_error(workspace, path):
    """Business mutation authority excludes repository/product metadata.

    Managed workspaces may themselves live under a state-home ancestor. Only
    the selected workspace's descendants are business inputs; an explicitly
    granted external target must not become an escape into another state tree.
    """
    from pathlib import Path
    root = Path(workspace).absolute()
    selected = Path(path)
    if not selected.is_absolute():
        selected = root / selected
    try:
        parts = selected.relative_to(root).parts
    except ValueError:
        parts = selected.parts
    if not any(protected_metadata_part(part) for part in parts):
        return None
    from backend.core.errors import error_payload
    return {**error_payload("PROTECTED_PATH"),
            "effect_state": "not_committed", "execution_state": "not_started"}
