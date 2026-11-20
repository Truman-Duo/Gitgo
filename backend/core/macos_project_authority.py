"""Explicit administrator renewal/revocation, separate from model tool RPC.

Each renewal rotates credentials and grant identity. A durable maintenance
marker closes admission across partial publication; retry creates fresh
credentials. Prior invocation mappings remain immutable recovery evidence.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid

from backend.core import macos_identity as identity
from backend.core import macos_helper_lifecycle as lifecycle
from backend.core import macos_broker_protocol as wire

API_VERSION=9
MAX_LIFETIME=86400
PENDING='pending-authorization.json'


def pending(root): return (root/'private'/PENDING).exists()


def lifetime_check(seconds):
    if type(seconds) is not int or not 10<=seconds<=MAX_LIFETIME:
        raise ValueError('Authorization lifetime must be 10–86400 seconds')


def sync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def clear_pending(root):
    path=root/'private'/PENDING
    if path.exists(): path.unlink();sync_directory(path.parent)


def publish_client(root,value):
    """Set ownership before publication; no window with a root-owned client."""
    target=root/'client.json';temp=root/('.client-'+uuid.uuid4().hex)
    fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(json.dumps(value).encode());stream.flush()
            os.fchown(stream.fileno(),value['identity'][2],-1);os.fsync(stream.fileno())
        os.replace(temp,target);sync_directory(root)
    finally:
        if temp.exists(): temp.unlink()


def supports_renewal(helper):
    # Do not apply new authority semantics to an old broker which lacks the
    # maintenance gate or the admission-time credential recheck.
    lifecycle.verify_bundle_path(helper)
    code='from backend.core.macos_project_authority import API_VERSION; print(API_VERSION)'
    result=subprocess.run([str(helper),'--gitgo-internal-role','python','-B','-I','-c',code],
        stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=15,
        env={'PATH':'/usr/bin:/bin','HOME':'/','PYTHONDONTWRITEBYTECODE':'1'})
    if result.returncode or result.stdout.strip()!=str(API_VERSION):
        raise ValueError('Installed helper does not support authorization renewal; upgrade it first')


def current_scope(root, *, after_reboot=False):
    from backend.core.macos_broker_service import grant
    value=grant(root,live=False)
    if value['boot']!=identity.boot_id() and not after_reboot:
        raise ValueError('Cross-boot authorization requires recovery; renewal refused')
    fd=wire.open_directory(value['project'])
    try:
        from backend.core.macos_project_identity import check_directory,check_persistent
        key=wire.directory_identity(fd)
        if value['boot']!=identity.boot_id():
            check_persistent(fd,value)
            # A newly mounted device number may change, the object may not.
            if key[1:]!=value['identity'][1:]:raise ValueError('Project inode or owner changed')
        else:check_directory(fd,value)
        if key[2]!=value['owner_uid']:
            raise ValueError('Authorized project directory was replaced or ownership changed')
        value={**value,'identity':key}
    finally: os.close(fd)
    return value


def renew(name,*,lifetime=3600,after_reboot=False):
    lifecycle.require_admin();lifetime_check(lifetime);root=lifecycle.service_root(name)
    with lifecycle.locked(root,'admin.lock'),lifecycle.locked(root,'admission.lock'):
        state=lifecycle.configuration(root)
        if state['phase']!='active': raise ValueError('Recover the service lifecycle before renewing authorization')
        supports_renewal(state['helper']);lifecycle.assert_idle(root)
        old=current_scope(root,after_reboot=after_reboot)
        from backend.core.macos_broker_service import raw_time
        token=secrets.token_hex(32)
        value={**old,'grant_id':uuid.uuid4().hex,'token_hash':hashlib.sha256(token.encode()).hexdigest(),
               'expires':raw_time()+lifetime,'revoked':False,'boot':identity.boot_id()}
        identity.atomic_json(root/'private'/PENDING,dict(previous=old['grant_id'],candidate=value['grant_id']))
        identity.atomic_json(root/'private/grant.json',{**old,'revoked':True})
        client=dict(socket=str(root/'broker.sock'),token=token,project=value['project'],
                    identity=value['identity'],grant_id=value['grant_id'])
        if 'project_object' in value:client['project_object']=value['project_object']
        publish_client(root,client)
        identity.atomic_json(root/'private/grant.json',value)
        clear_pending(root)  # Admission opens only after all durable writes.
        return dict(grant_id=value['grant_id'],owner_uid=value['owner_uid'],project=value['project'],
                    lifetime=lifetime,authorization_live=True,client_config=str(root/'client.json'))


def revoke(name,*,cleanup_timeout=20):
    lifecycle.require_admin();root=lifecycle.service_root(name)
    with lifecycle.locked(root,'admin.lock'):
        state=lifecycle.configuration(root)
        if state['phase']!='active': raise ValueError('Recover the service lifecycle before revocation')
        supports_renewal(state['helper'])
        from backend.core.macos_broker_service import grant
        value=grant(root,live=False)
        # Do not take the execution lock until after revoking: the running
        # broker must be able to observe this and stop its detached descendants.
        identity.atomic_json(root/'private/grant.json',{**value,'revoked':True})
        clear_pending(root)
        end=time.monotonic()+cleanup_timeout
        while True:
            try:
                with lifecycle.locked(root,'admission.lock'): lifecycle.assert_idle(root)
                return dict(grant_id=value['grant_id'],authorization_live=False,cleanup_confirmed=True)
            except (RuntimeError,FileNotFoundError) as exc:
                if time.monotonic()>=end:
                    raise RuntimeError('Authorization revoked; cleanup remains unconfirmed: '+str(exc)) from exc
                time.sleep(.1)
