"""One-shot native checkpoint coordinator beneath admitted worker ownership.

Callers need selected-runtime/Host admission for the complete lifetime. This
class joins existing native owners and repository SQL; it cannot admit itself,
restore a preparation ticket, or authorize execution from retained bytes.
"""
from contextlib import contextmanager
import uuid

from backend.core.storage.host_records import require
from backend.core.storage.native_windows_cas import (
    WindowsCasWriterLibrary,WindowsCasGenerationLibrary,WindowsCasWriter,
    WindowsCasGeneration,CasPurpose,CasFileIntent,
)
from backend.core.storage.project_namespace import ProjectNamespace
from .batch_records import BatchContext,BatchPreparation
from . import batch_sql as batch
from . import batch_inventory as inventory
from .write_window import WriteWindow


class NativeCheckpoint:
    def __init__(self,namespace,db,writers,readers,context,window,catalogue):
        require(type(namespace) is ProjectNamespace and type(writers) is WindowsCasWriterLibrary
                and type(readers) is WindowsCasGenerationLibrary and type(context) is BatchContext
                and type(window) is WriteWindow and window.db is db,
                'exact native checkpoint owners required')
        namespace.require_live()
        require(context.host==namespace.anchor.binding and context.project==namespace.project
                and context.store==namespace.record.identity.store and context.profile==namespace.record.profile
                and context.parent==dict(namespace.record.identity.members)['data/cas'],
                'checkpoint context and actual namespace differ')
        self.ns,self.db,self.writers,self.readers=namespace,db,writers,readers
        self.context,self.window,self.catalogue=context,window,catalogue
        self._started=False;self.preparation=None;self.records=();self.pins=[]

    @contextmanager
    def transaction(self):
        self.ns.require_live()
        with self.window.transaction():
            yield
            inventory.require_cleanup_capacity(self.db,self.catalogue,
                self.window.grant.bound.max_pages,
                future_payload_bytes=8*512*4+32*64*3+64*2)

    def publish(self,publication):
        require(not self._started,'checkpoint creation attempt already consumed')
        self._started=True
        intents=tuple(CasFileIntent(member.digest,str(uuid.uuid4()),len(member.data))
                      for member in publication.objects.members)
        plan=BatchPreparation(self.context,publication,intents)
        self.preparation=plan
        with self.transaction():batch.prepare(self.db,plan)
        # Only a known fresh preparation COMMIT reaches this point. Any failure
        # leaves _started consumed and all acquired native owners under Host.
        records=[]
        for ordinal,(intent,member) in enumerate(zip(intents,publication.objects.members)):
            writer=self.ns.host._new(WindowsCasWriter,self.writers)
            birth=writer.create(self.ns.handles['data/cas'],self.context.parent,intent)
            with self.transaction():batch.birth(self.db,plan,ordinal,birth)
            sealed=writer.materialize(member.data)
            require(sealed==birth,'materialized object differs from durable birth identity')
            with self.transaction():batch.seal(self.db,plan,ordinal,sealed)
            require(self.ns.host._release(writer),'CAS writer closure is incomplete')
            records.append(sealed)
        self.records=tuple(records)
        for record,member in zip(self.records,publication.objects.members):
            reader=self.ns.host._new(WindowsCasGeneration,self.readers,CasPurpose.READ)
            require(reader.open(self.ns.handles['data/cas'],self.context.parent,record),
                    'sealed object disappeared before publication')
            require(reader.read()==member.data,'sealed object bytes changed before publication')
            self.pins.append(reader)
        with self.transaction():batch.publish(self.db,plan,self.records)
        # Original pins and preparation debt remain held. The parent's business
        # result requires independent committed verification after retirement.
        return plan
