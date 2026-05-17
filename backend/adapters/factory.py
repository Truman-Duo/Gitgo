"""适配器工厂 — 根据 RepoNode.file_access.kind 创建适配器对"""

from __future__ import annotations

from pathlib import Path

from backend.adapters import FileAdapter, GitRunner, LocalFileAdapter, LocalGitRunner
from backend.models import FileAccessKind, RepoNode


def create_adapters_for_node(
    node: RepoNode,
) -> tuple[FileAdapter, GitRunner]:
    """根据 node.file_access.kind 创建适配器对。

    返回 (FileAdapter, GitRunner)，共享同一 SSH 连接（若为 SSH 节点）。
    LOCAL 节点: LocalFileAdapter + LocalGitRunner
    SSH  节点: SSHFileAdapter + SSHGitRunner
    """
    access = node.file_access
    path = access.path or str(Path.cwd())

    if access.kind == FileAccessKind.SSH:
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        from backend.adapters.ssh_git_runner import SSHGitRunner

        fa: FileAdapter = SSHFileAdapter(
            host=access.host,
            port=access.port,
            username=access.username,
            key_path=access.key_path,
            root=path,
        )
        gr: GitRunner = SSHGitRunner(
            host=access.host,
            port=access.port,
            username=access.username,
            key_path=access.key_path,
            repo_path=path,
        )
        return fa, gr

    if access.kind == FileAccessKind.SMB:
        from backend.adapters.smb_file_adapter import SMBFileAdapter

        share = access.share or access.host
        fa = SMBFileAdapter(
            host=access.host,
            share=share,
            root=path,
            username=access.username,
            port=access.port or 445,
        )
        gr: GitRunner = LocalGitRunner(Path(fa.unc_path))
        return fa, gr

    # LOCAL（默认）
    resolved = Path(path).resolve()
    return LocalFileAdapter(resolved), LocalGitRunner(resolved)
