"""Closed preparation values for qualification, not an execution grant.

Only the admitted coordinator can turn a known committed fresh preparation
into a file-creation capability. Decoding retained values never creates one.
"""
from dataclasses import asdict, dataclass
import hashlib
import json

from backend.core.sandbox_read import ObjectIdentity
from backend.core.storage.checkpoint_work import CheckpointPublication
from backend.core.storage.fact_record import RuntimeFactRecord
from backend.core.storage.host_records import guid, identity, require
from backend.core.storage.native_windows_cas import CasFileIntent
from backend.core.storage.runtime_lifecycle import RuntimeReference
from backend.core.storage.runtime_manifest import canonical, digest

MAX_FACT_BYTES = 65536
MAX_CONTEXT_BYTES = 4096
MAX_MEMBERS = 8
MAX_GENERATIONS = 32
# Reduced delivery is serial. A previous preparation must settle before the
# next publication; concurrent preparations are explicitly deferred.
MAX_PREPARATIONS = 1


def decode(raw, limit):
    require(type(raw) is bytes and 0 < len(raw) <= limit, "bounded retained batch encoding")
    def pairs(items):
        result = {}
        for key,value in items:
            require(key not in result, "duplicate retained batch field")
            result[key] = value
        return result
    def nonfinite(value):
        require(False, "nonfinite retained batch value")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, RecursionError, UnicodeError) as error:
        raise ValueError("Invalid retained batch encoding") from error
    require(canonical(value) == raw, "canonical retained batch encoding required")
    return value


@dataclass(frozen=True)
class BatchContext:
    host: str
    project: str
    store: str
    slot: int
    profile: str
    runtime: RuntimeReference
    epoch: int
    request: str
    launch: str
    pid: int
    created: int
    parent: ObjectIdentity

    def __post_init__(self):
        digest(self.host);digest(self.profile)
        for value in (self.project,self.store,self.request,self.launch): guid(value)
        require(type(self.runtime) is RuntimeReference, "exact selected runtime binding")
        require(type(self.slot) is int and 0 <= self.slot < 11, "exact Host project slot")
        require(type(self.epoch) is int and 0 < self.epoch < (1 << 63), "finite storage epoch")
        require(type(self.pid) is int and 0 < self.pid < (1 << 32)
                and type(self.created) is int and 0 < self.created < (1 << 64),
                "exact admitted native worker identity")
        identity(self.parent)

    def to_bytes(self):
        raw=canonical(dict(version=1,**asdict(self)))
        require(len(raw)<=MAX_CONTEXT_BYTES,"bounded complete batch context")
        return raw

    @classmethod
    def from_bytes(cls,raw):
        value=decode(raw,MAX_CONTEXT_BYTES)
        require(type(value) is dict and set(value)=={
            "version","host","project","store","slot","profile","runtime","epoch",
            "request","launch","pid","created","parent"} and type(value["version"]) is int
            and value["version"]==1,"closed complete batch context")
        runtime,parent=value["runtime"],value["parent"]
        require(type(runtime) is dict and set(runtime)=={"generation","manifest","epoch"},
                "closed runtime binding")
        require(type(parent) is dict and set(parent)=={"platform","volume","file_id"},
                "closed native parent binding")
        result=cls(**{k:v for k,v in value.items() if k not in {"version","runtime","parent"}},
                   runtime=RuntimeReference(**runtime),parent=ObjectIdentity(**parent))
        require(result.to_bytes()==raw,"canonical batch context")
        return result


@dataclass(frozen=True)
class BatchPreparation:
    context: BatchContext
    publication: CheckpointPublication
    intents: tuple[CasFileIntent, ...]

    def __post_init__(self):
        require(type(self.context) is BatchContext and type(self.publication) is CheckpointPublication,
                "exact immutable context and repository publication")
        require(self.publication.limits is not None, "qualified bounded publication required")
        require(all(fact.project_id==self.context.project
                    and fact.process_id==self.publication.process_id
                    and fact.session_id==self.publication.session_id
                    for fact in self.publication.facts), "publication and admitted project identities differ")
        self.publication.limits.require_publication(self.publication.writes,self.publication.objects)
        require(type(self.intents) is tuple and 1<=len(self.intents)<=MAX_MEMBERS
                and len(self.intents)==len(self.publication.objects.members)
                and all(type(value) is CasFileIntent for value in self.intents),
                "complete immutable generation intents")
        require(len({value.generation for value in self.intents})==len(self.intents),
                "one unique generation per planned object")
        for intent,obj in zip(self.intents,self.publication.objects.members):
            intent.require_bytes(obj.data)
        require(len(self.fact_bytes)<=MAX_FACT_BYTES, "complete bounded retained facts")

    @property
    def fact_bytes(self):
        return canonical([fact.parameters for fact in self.publication.facts])


def facts_from_bytes(raw):
    values=decode(raw,MAX_FACT_BYTES)
    require(type(values) is list and 1<=len(values)<=2
            and all(type(row) is list and len(row)==12 for row in values),
            "complete bounded checkpoint facts")
    result=tuple(RuntimeFactRecord(*row) for row in values)
    require(len({row.event_id for row in result})==len(result),"unique retained facts")
    return result


def identity_bytes(value):
    identity(value)
    return canonical(asdict(value))


def identity_from_bytes(raw):
    value=decode(raw,256)
    require(type(value) is dict and set(value)=={"platform","volume","file_id"},
            "closed retained birth identity")
    result=ObjectIdentity(**value);identity(result)
    return result

