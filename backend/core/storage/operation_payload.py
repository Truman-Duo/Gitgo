"""Closed bounded messages; values never supply paths, SQL or native authority."""
import json
from .host_records import require
from .journal_cells import canonical
from .operational_profile import OperationalProfile

def parse_payload(raw,*,maximum=16384):
    require(type(raw) is bytes and 0<len(raw)<=maximum,'bounded operation encoding required')
    try:value=json.loads(raw)
    except (ValueError,UnicodeError,RecursionError) as error:
        from .models import StorageBlocked
        raise StorageBlocked('operation_encoding_invalid') from error
    require(type(value) is dict and canonical(value)==raw,'canonical closed operation object required')
    return value

def payload_bytes(mode,value):
    require(type(mode) is str and mode in OperationalProfile().operations,'closed operational mode')
    copied=OperationalProfile().checkpoint_limits.copy_snapshot(value)
    fields=({'snapshot','workspace_hint','created_at'} if mode=='publish_checkpoint' else
            {'process_id'} if mode=='read_checkpoint' else set())
    require(set(copied)==fields,'closed operation payload fields')
    if mode=='publish_checkpoint':
        require(type(copied['snapshot']) is dict,'checkpoint snapshot required')
        revision=copied['snapshot'].get('expected_revision')
        require(type(revision) is int and 0<=revision<(1<<63),'protected publication requires an explicit observed revision')
        for name in ('workspace_hint','created_at'):
            require(type(copied[name]) is str and 0<len(copied[name].encode('utf8'))<=8192,'bounded registration metadata')
    elif mode=='read_checkpoint':
        require(type(copied['process_id']) is str and 0<len(copied['process_id'].encode('utf8'))<=8192,'bounded process selector')
    raw=canonical(copied)
    require(len(raw)<=16384,'operation input exceeds explicit encoding limit')
    return raw
