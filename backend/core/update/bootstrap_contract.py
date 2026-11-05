"""Immutable-launcher capabilities shared by provisioning and runtime probes."""
import os
WINDOWS_API = 10
POSIX_API = 11
CURRENT_API = WINDOWS_API if os.name == "nt" else POSIX_API
SUPPORTED_APIS = (7, 8, WINDOWS_API) if os.name == "nt" else (6, 9, POSIX_API)
AUTOMATIC_EXIT_APIS = (8, 9, WINDOWS_API, POSIX_API)
ENGINE_DELEGATION_APIS = (WINDOWS_API, POSIX_API)
