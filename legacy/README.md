# Legacy archive

This directory records product lines that are no longer part of the Gitgo
terminal release.

## Published source archive

`source/qt-git-manager/` contains the early Qt/Rich Git-management clients,
their historical build script, debug launcher, and original icons.  The code
is retained for reference only.  It is not imported by the current launcher,
tested as a supported product, or included in the terminal installer.

## Publication boundary

Private engineering records, historical executables and local configuration
are excluded from the published tree. Runtime databases, project content,
provider credentials and user workspaces do not belong in a source commit.
Check candidate contents and references before publication; passing a scan
alone does not establish that all material is public.

The supported Windows build starts at `packaging/build_windows.ps1`.
