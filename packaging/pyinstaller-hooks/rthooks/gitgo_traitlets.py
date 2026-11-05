"""Keep traitlets' frozen-source compatibility outside handle-only helpers."""
import sys


def _initialize():
    if (sys.platform == "win32" and len(sys.argv) >= 3
            and sys.argv[1] == "--gitgo-internal-role"
            and sys.argv[2] in {"windows-scope", "windows-job-lease"}):
        return
    import traitlets.traitlets

    # Frozen modules have no source for traitlets' deprecation inspection.
    def no_source_warning(method, cls, method_name, message):
        return None

    traitlets.traitlets._deprecated_method = no_source_warning


_initialize()
del _initialize
