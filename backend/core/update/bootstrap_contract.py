"""Immutable-launcher capabilities shared by provisioning and runtime probes."""
import os
import sys
WINDOWS_API = 10
POSIX_API = 11
MACOS_QUIT_API = 12
MACOS_PROBE_BROKER_API = 13
CURRENT_API = WINDOWS_API if os.name == "nt" else MACOS_PROBE_BROKER_API if sys.platform == "darwin" else POSIX_API
SUPPORTED_APIS = (7, 8, WINDOWS_API) if os.name == "nt" else (
    (6, 9, POSIX_API, MACOS_QUIT_API, MACOS_PROBE_BROKER_API) if sys.platform == "darwin" else (6, 9, POSIX_API)
)
AUTOMATIC_EXIT_APIS = (8, 9, WINDOWS_API, POSIX_API, MACOS_QUIT_API, MACOS_PROBE_BROKER_API)
ENGINE_DELEGATION_APIS = (WINDOWS_API, POSIX_API, MACOS_PROBE_BROKER_API)
