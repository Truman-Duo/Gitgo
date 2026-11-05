"""Preserve PyInstaller's distutils setup for application/payload roles.

The handle-only Windows Job helpers use only the standard library. Loading the
packaging stack in those processes adds latency to every bounded Git query.
"""
import sys


def _initialize():
    if (sys.platform == "win32" and len(sys.argv) >= 3
            and sys.argv[1] == "--gitgo-internal-role"
            and sys.argv[2] in {"windows-scope", "windows-job-lease"}):
        return
    import os
    import setuptools

    default = "local" if int(setuptools.__version__.partition(".")[0]) >= 60 else "stdlib"
    if os.environ.get("SETUPTOOLS_USE_DISTUTILS", default) == "local":
        import _distutils_hack
        _distutils_hack.add_shim()


try:
    _initialize()
except Exception:
    # Match the bundled PyInstaller hook's optional compatibility behavior.
    pass
del _initialize
