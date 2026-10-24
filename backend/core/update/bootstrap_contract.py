"""Immutable-launcher capabilities shared by provisioning and runtime probes."""
import os
WINDOWS_API = 8
POSIX_API = 9
CURRENT_API = WINDOWS_API if os.name == "nt" else POSIX_API
SUPPORTED_APIS = (7, WINDOWS_API) if os.name == "nt" else (6, POSIX_API)
AUTOMATIC_EXIT_APIS = (WINDOWS_API, POSIX_API)
