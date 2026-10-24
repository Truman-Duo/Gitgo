"""Paths for developer-owned artifacts; never a product storage authority."""
from pathlib import Path
import os


def development_root(repository: Path) -> Path:
    base = Path(repository).resolve()
    configured = os.getenv("GITGO_DEV_ARTIFACTS")
    output = Path(configured).expanduser() if configured else base / "dev-artifacts"
    if not output.is_absolute():
        output = base / output
    output = output.resolve()
    if {".gitgo", ".git"}.intersection(part.casefold() for part in output.parts) or output == base:
        raise ValueError("Developer artifacts cannot use product .gitgo, Git metadata, or the repository root")
    if output in base.parents:
        raise ValueError("Developer artifacts cannot use an ancestor of the repository")
    return output


def test_state_home(workspace: Path) -> Path:
    """Select the isolated project's product-owned store; do not create it."""
    return Path(workspace).resolve() / ".gitgo" / "state"


def owned_development_path(repository: Path, value: Path) -> Path:
    """Reject stale manifests that would recreate artifacts in product storage."""
    path = Path(value).resolve()
    if not path.is_relative_to(development_root(repository)):
        raise ValueError("The manifest targets a location outside this developer artifact root")
    return path
