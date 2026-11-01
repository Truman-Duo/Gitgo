"""Bounded JSON edits to a saved program; the normal compiler owns validity."""
from copy import deepcopy
import re
from .contracts import bounded_json


def _index(part, size, *, insert=False):
    if part=='-' and insert:
        return size
    if not re.fullmatch(r'0|[1-9][0-9]*',part):
        raise ValueError('MECHANISM_REVISION_INDEX_INVALID')
    value=int(part)
    if value>=size+(1 if insert else 0):
        raise ValueError('MECHANISM_REVISION_INDEX_MISSING')
    return value


def revise_program(compiled, changes, source_refs):
    if not isinstance(changes,list) or not 1<=len(changes)<=32:
        raise ValueError('MECHANISM_REVISION_CHANGES_REQUIRED')
    bounded_json(changes)
    raw={key:deepcopy(compiled[key]) for key in ('id','intent','scope','bindings')}
    raw.update(schema_version=2,source_refs=deepcopy(source_refs),elements=[
        {key:deepcopy(node[key]) for key in ('id','use','version','parameters')} for node in compiled['elements']])
    for change in changes:
        if (not isinstance(change,dict) or change.get('op') not in {'add','remove','replace'}
                or set(change)-{'op','path','value'} or ('value' in change)!=(change['op']!='remove')):
            raise ValueError('MECHANISM_REVISION_CHANGE_INVALID')
        path=change.get('path')
        if not isinstance(path,str) or not path.startswith('/') or len(path)>512 or re.search(r'~(?![01])',path):
            raise ValueError('MECHANISM_REVISION_PATH_INVALID')
        parts=[p.replace('~1','/').replace('~0','~') for p in path[1:].split('/')]
        if len(parts)>16 or parts[0] not in {'intent','elements','bindings'}:
            raise ValueError('MECHANISM_REVISION_IDENTITY_PROTECTED')
        parent=raw
        for part in parts[:-1]:
            if isinstance(parent,list):parent=parent[_index(part,len(parent))]
            elif isinstance(parent,dict) and part in parent:parent=parent[part]
            else:raise ValueError('MECHANISM_REVISION_PARENT_MISSING')
        key=parts[-1];operation=change['op']
        if isinstance(parent,list):
            index=_index(key,len(parent),insert=operation=='add')
            if operation=='add':parent.insert(index,deepcopy(change['value']))
            elif operation=='replace':parent[index]=deepcopy(change['value'])
            else:del parent[index]
        elif isinstance(parent,dict):
            if operation!='add' and key not in parent:
                raise ValueError('MECHANISM_REVISION_TARGET_MISSING')
            if operation=='remove':del parent[key]
            else:parent[key]=deepcopy(change['value'])
        else:raise ValueError('MECHANISM_REVISION_CONTAINER_REQUIRED')
    return bounded_json(raw,maximum=262144)
