"""Read the existing session repository through exact committed CAS ownership.

The enclosing admitted native worker owns the shared
State snapshot and exclusive project/CAS leases for the entire repository read.
This adapter cannot admit a worker, recover debt or construct a storage path.
"""
import hashlib
import json
from types import SimpleNamespace
import threading

from backend.core.storage.host_records import require
from backend.core.storage.checkpoint_limits import CheckpointLimits
from backend.core.storage.native_windows_cas import (
    WindowsCasGenerationLibrary,WindowsCasGeneration,CasPurpose,CasFileIntent,CasFileRecord,
)
from backend.core.storage.project_namespace import ProjectNamespace
from backend.core.storage.repositories.sessions import load_agent_session_state
from .batch_records import identity_from_bytes
from .batch_sql import fetchone,transaction


class CommittedReader:
    def __init__(self,namespace,db,generations,limits):
        require(type(namespace) is ProjectNamespace and type(generations) is WindowsCasGenerationLibrary,
                'exact native namespace and generation library required')
        require(type(limits) is CheckpointLimits,'exact qualified read limits required')
        namespace.require_live();transaction(db)
        self.namespace,self.db,self.generations,self.limits=namespace,db,generations,limits
        self.parent=dict(namespace.record.identity.members)['data/cas']
        self.cache={}

    def read_json(self,reference,*,repair=False):
        require(repair is False,'committed reader cannot repair missing objects')
        self.namespace.require_live();transaction(self.db)
        require(type(reference) is str and len(reference)==71 and reference.startswith('sha256:'),
                'exact committed reference required')
        digest=reference[7:]
        # This validates lowercase digest syntax without deriving a filename.
        from backend.core.storage.runtime_manifest import digest as require_digest
        require_digest(digest)
        if digest in self.cache:
            data=self.cache[digest]
        else:
            require(len(self.cache)<32,'finite native read closure exceeded')
            row=fetchone(self.db,
                'SELECT g.generation,g.digest,g.byte_length,g.birth,g.phase,o.byte_length '
                'FROM storage_cas_current AS c '
                'JOIN storage_cas_generations AS g ON g.generation=c.generation '
                'JOIN objects AS o ON o.digest=c.digest WHERE c.digest=?',(digest,))
            require(row is not None and row[1]==digest and row[4]=='LIVE'
                    and type(row[2]) is int and row[2]==row[5],
                    'committed logical and physical object ownership required')
            intent=CasFileIntent(row[1],row[0],row[2])
            record=CasFileRecord(intent,identity_from_bytes(row[3]))
            owner=self.namespace.host._new(WindowsCasGeneration,self.generations,CasPurpose.READ)
            require(owner.open(self.namespace.handles['data/cas'],self.parent,record),
                    'committed object is absent')
            data=owner.read()
            require(hashlib.sha256(data).hexdigest()==digest,'committed object bytes changed')
            require(self.namespace.host._release(owner),'native reader closure is incomplete')
            self.cache[digest]=data
        # Reparse the bounded immutable bytes on cache hits: repository mutation
        # of a returned dict must never mutate another logical read's result.
        value=json.loads(data)
        return self.limits.copy_snapshot(value)

    def session(self,process_id):
        self.namespace.require_live();transaction(self.db)
        require(type(process_id) is str and 0<len(process_id)<=self.limits.scalar_bytes,
                'bounded session selector required')
        require(len(process_id.encode('utf8'))<=self.limits.scalar_bytes,'bounded session selector bytes')
        facade=SimpleNamespace(_state=self.db,_state_lock=threading.RLock(),
                               _ensure_open=self.namespace.require_live,_read_json_blob=self.read_json)
        result=load_agent_session_state(facade,process_id)
        return None if result is None else self.limits.copy_snapshot(result)
