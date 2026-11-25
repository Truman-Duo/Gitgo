"""Clean one Gitgo profile and, by request, verified workspace metadata."""

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from backend.core.config import ConfigManager
from backend.core.host_profile_lease import HostProfileLease
from backend.core.secret_store import EncryptedSecretStore
from backend.core.storage.paths import _existing_project_identity, _default_state_home
from backend.models import FileAccessKind


CONFIG_NAMES = ('config.json', 'commit-config.json', 'llm_config.json', 'provider_secrets.json')


def checked(path):
    """Identify a local entry without accepting links in any path component."""
    if not path.is_absolute() or '..' in path.parts or (os.name == 'nt' and path.anchor.startswith('\\\\')):
        raise ValueError(f'Unsafe path: {path}')
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'Redirected path: {part}')
    return info


def regular_file(path):
    try:
        info = checked(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f'Expected an ordinary Gitgo file: {path}')
    return True


def metadata_identity(workspace, workspaces):
    metadata = workspace / '.gitgo'
    info = checked(metadata)
    if not stat.S_ISDIR(info.st_mode) or os.path.ismount(metadata):
        raise ValueError('Project metadata is not a local directory')
    if any(metadata == path or metadata in path.parents for path in workspaces):
        raise ValueError('Project metadata contains a registered workspace')
    marker = _existing_project_identity(workspace)
    if marker is None or not regular_file(marker) or marker.stat().st_size > 64:
        raise ValueError('No verified Gitgo project identity')
    project_id = str(uuid.UUID(marker.read_text(encoding='ascii').strip()))
    stack = [metadata]
    while stack:
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                child = entry.stat(follow_symlinks=False)
                if (entry.name.casefold() == '.git' or entry.is_symlink()
                        or getattr(child, 'st_file_attributes', 0) & 0x400
                        or (os.name != 'nt' and child.st_dev != info.st_dev)):
                    raise ValueError('Project metadata contains a repository or redirected entry')
                if stat.S_ISDIR(child.st_mode):
                    if os.name != 'nt' and os.path.ismount(entry.path):
                        raise ValueError('Project metadata contains a mount point')
                    stack.append(entry.path)
    return (info.st_dev, info.st_ino, project_id)


def plan(profile, include_metadata):
    files = [profile / name for name in CONFIG_NAMES if regular_file(profile / name)]
    projects = {}
    skipped = []
    if include_metadata and profile / 'config.json' in files:
        config = ConfigManager.load(profile / 'config.json', strict=True)
        workspaces = {Path(node.file_access.path).expanduser() for project in config.projects
                      for node in (project.workspace, project.release, project.trial)
                      if node is not None and node.file_access.kind == FileAccessKind.LOCAL and node.file_access.path}
        for workspace in sorted(workspaces):
            metadata = workspace / '.gitgo'
            try:
                if workspace == Path.home() or profile == metadata or metadata in profile.parents:
                    raise ValueError('This path contains the user profile')
                projects[metadata] = metadata_identity(workspace, workspaces)
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as error:
                skipped.append(str(metadata) + ' (' + str(error) + ')')
    record = {
        'files': [str(path) for path in files],
        'metadata': [str(path) for path in projects],
        'skipped': skipped,
    }
    snapshot = [(str(path), path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_size) for path in files]
    identities = [(str(path), value) for path, value in projects.items()]
    record['token'] = hashlib.sha256(json.dumps([snapshot, identities, skipped]).encode()).hexdigest()
    return record, files, projects


def cleanup(profile, include_metadata, expected, state_homes):
    with ExitStack() as leases:
        for state_home in dict.fromkeys(state_homes):
            if (state_home / 'dashboard-owner.lock').exists():
                checked(state_home / 'dashboard-owner.lock')
                leases.enter_context(HostProfileLease(state_home))
        record, files, metadata = plan(profile, include_metadata)
        if expected is not None and record['token'] != expected:
            raise ValueError('Gitgo configuration changed after confirmation. Review the cleanup again.')
        index = profile / 'provider_secrets.json'
        if index in files:
            EncryptedSecretStore(index).clear()
        for path, identity in metadata.items():
            if metadata_identity(path.parent, set()) != identity:
                raise ValueError(f'Project metadata changed after confirmation: {path}')
            shutil.rmtree(path)
        # Keep the project registry as the retry inventory until the other removals succeed.
        for path in sorted(files, key=lambda item: item.name == 'config.json'):
            if regular_file(path):
                path.unlink()
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preview', 'clean'))
    parser.add_argument('--project-metadata', action='store_true')
    parser.add_argument('--expect')
    parser.add_argument('--profile-id')
    args = parser.parse_args()
    profile = Path.home() / '.gitgo'
    if args.profile_id:
        if not re.fullmatch('[a-f0-9]{32}', args.profile_id):
            parser.error('Invalid installation profile ID')
        profile = profile / 'setup-profiles' / args.profile_id
    if args.action == 'preview':
        record = plan(profile, args.project_metadata)[0]
    else:
        state_homes = [profile / 'state'] if args.profile_id else [_default_state_home()]
        if os.name == 'nt' and not args.profile_id and os.environ.get('LOCALAPPDATA'):
            state_homes.extend((profile / 'state', Path(os.environ['LOCALAPPDATA']) / 'Gitgo' / 'state'))
        record = cleanup(profile, args.project_metadata, args.expect, state_homes)
    print(json.dumps(record, ensure_ascii=False))


if __name__ == '__main__':
    main()
