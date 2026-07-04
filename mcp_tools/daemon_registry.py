"""Daemon registry — singleton manager for per-project DaemonClient instances.

MCP tools are stateless (each call is independent). This module provides a
module-level cache so DaemonClient subprocesses are reused across calls.
"""

from __future__ import annotations

import threading

from backend.core.daemon.client import DaemonClient

_clients: dict[str, DaemonClient] = {}
_lock = threading.Lock()


def get_client(project_name: str) -> DaemonClient:
    """Get or create a DaemonClient for the given project.

    Automatically starts the daemon if not already running.
    """
    with _lock:
        client = _clients.get(project_name)
        if client is None or not client.is_running():
            client = DaemonClient(project_name)
            client.start()
            _clients[project_name] = client
        return client


def shutdown_all() -> None:
    """Stop all daemon clients. Call on MCP server shutdown."""
    with _lock:
        for name, client in list(_clients.items()):
            try:
                client.stop()
            except Exception:
                pass
        _clients.clear()
