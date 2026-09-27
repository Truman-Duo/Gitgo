# Legacy archive

This directory records product lines that are no longer part of the Gitgo
terminal release.

## Published source archive

`source/qt-git-manager/` contains the early Qt/Rich Git-management clients,
their historical build script, debug launcher, and original icons.  The code
is retained for reference only.  It is not imported by the current launcher,
tested as a supported product, or included in the terminal installer.

## Local-only archive

The following paths are intentionally ignored by Git:

- `artifacts/`: historical executables, PyInstaller work directories, spec
  files, and local configuration copied from old distributions.
- `docs/`: superseded plans and private engineering history.

These files may contain machine-local paths, configuration, or obsolete
implementation details.  They must pass the release privacy scan before any
part is moved back into the published tree.  Runtime databases, project
content, provider credentials, and user workspaces never belong in this
archive or in a release commit.

The supported Windows build starts at `packaging/build_windows.ps1`.
