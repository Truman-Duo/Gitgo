"""Input and historical-work limits for a future qualified checkpoint profile.

There are no production defaults and the legacy writer does not enable these
limits. A backend must bind them to its admitted resource envelope. Passing
these checks is not a durable ticket or proof of SQLite/native heap bounds.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from .models import StorageBlocked


def _require(condition, name):
    if not condition:
        raise StorageBlocked('checkpoint_limit_'+name)


@dataclass(frozen=True)
class CheckpointLimits:
    input_nodes: int
    input_depth: int
    input_bytes: int
    scalar_bytes: int
    sql_statements: int
    sql_parameter_bytes: int
    cas_objects: int
    cas_bytes: int
    history_rows: int
    reference_rows: int
    replacement_rows: int

    def __post_init__(self):
        from dataclasses import fields
        for field in fields(self):
            value = getattr(self,field.name)
            _require(type(value) is int and 0 < value <= (1 << 31)-1,field.name)
        _require(self.input_depth <= 64,'input_depth')
        _require(self.scalar_bytes <= self.input_bytes,'scalar_bytes')

    def copy_snapshot(self, snapshot):
        """Bound before copying/encoding, without adapters or user callbacks.

        Shared subtrees are charged for each occurrence; cycles are refused.
        The budget counts keys, values and container entries, so millions of
        empty strings/containers cannot evade a byte-only check. These are
        finite work/input bounds, not measurements of Python heap allocation.
        """
        _require(type(snapshot) is dict,'snapshot_type')
        nodes = size = 0
        active = set()
        def visit(value, depth):
            nonlocal nodes,size
            nodes += 1
            _require(nodes <= self.input_nodes,'input_nodes')
            _require(depth <= self.input_depth,'input_depth')
            kind = type(value)
            if kind in (dict,list,tuple):
                _require(id(value) not in active,'input_cycle')
                # Each entry consumes at least one further node; dictionaries
                # consume one key and one value. Refuse before building output.
                width = len(value)*(2 if kind is dict else 1)
                _require(width <= self.input_nodes-nodes,'input_nodes')
                size += 8+width*8
                _require(size <= self.input_bytes,'input_bytes')
                active.add(id(value))
                try:
                    if kind is dict:
                        result = {}
                        for key,item in value.items():
                            _require(type(key) is str,'key_type')
                            result[visit(key,depth+1)] = visit(item,depth+1)
                        return result
                    items = [visit(item,depth+1) for item in value]
                    return tuple(items) if kind is tuple else items
                finally:
                    active.remove(id(value))
            if kind is str:
                _require(len(value) <= self.scalar_bytes,'scalar_bytes')
                try:
                    amount = len(value.encode('utf8',errors='strict'))
                except UnicodeError as error:
                    raise StorageBlocked('checkpoint_limit_text_encoding') from error
                _require(amount <= self.scalar_bytes,'scalar_bytes')
            elif value is None or kind is bool:
                amount = 1
            elif kind is int:
                _require(-(1 << 63) <= value < (1 << 63),'integer')
                amount = 8
            elif kind is float:
                _require(math.isfinite(value),'number')
                amount = 8
            else:
                raise StorageBlocked('checkpoint_limit_input_type')
            size += amount
            _require(size <= self.input_bytes,'input_bytes')
            return value
        return visit(snapshot,0)

    def require_publication(self, writes, objects):
        _require(len(writes.statements) <= self.sql_statements,'sql_statements')
        _require(writes.parameter_bytes <= self.sql_parameter_bytes,'sql_parameter_bytes')
        _require(len(objects.members) <= self.cas_objects,'cas_objects')
        _require(objects.byte_length <= self.cas_bytes,'cas_bytes')
        for statement in writes.statements:
            for value in statement.parameters:
                if type(value) is str:
                    _require(len(value) <= self.scalar_bytes,'scalar_bytes')
                    _require(len(value.encode('utf8')) <= self.scalar_bytes,'scalar_bytes')
                elif type(value) is bytes:
                    _require(len(value) <= self.scalar_bytes,'scalar_bytes')

    def require_transition(self, footprint, objects, messages, dependencies, terminal):
        _require(messages <= self.replacement_rows, 'new_messages')
        _require(dependencies <= self.replacement_rows, 'new_dependencies')
        _require(objects <= self.replacement_rows, 'new_references')
        _require(footprint.total_events+int(terminal) <= self.history_rows, 'new_events')
        _require(footprint.total_facts+1+int(terminal) <= self.history_rows, 'new_facts')
        _require(footprint.total_references-footprint.checkpoint_references+2*objects
                 <= self.reference_rows, 'new_roots')


@dataclass(frozen=True)
class CheckpointFootprint:
    """Bounded old-row counts, not an authoritative state revision or quota."""
    messages: int
    dependencies: int
    checkpoint_references: int
    total_events: int
    total_facts: int
    total_references: int

    def __post_init__(self):
        from dataclasses import fields
        for field in fields(self):
            value = getattr(self, field.name)
            _require(type(value) is int and 0 <= value < (1 << 63), 'footprint')


def capture_footprint(connection, limits: CheckpointLimits, process_id, session_id):
    """One snapshot and one SELECT; each count visits at most limit+1 rows.

    Full event/outbox/reference counts also bound the later legacy correlated
    scans. Indexed replacement predicates use the actual State schema keys.
    The qualified owner must cap pages and query instructions independently.
    Do not drop history or roots to make an existing store fit this profile.
    """
    _require(type(limits) is CheckpointLimits,'profile_type')
    fields = (
        ('messages','session_id=?',(session_id,),limits.replacement_rows),
        ('process_dependencies','process_id=?',(process_id,),limits.replacement_rows),
        ('object_refs',"owner_type='session_checkpoint' AND owner_id=?",(session_id,),limits.replacement_rows),
        ('session_events','1',(),limits.history_rows),
        ('runtime_outbox','1',(),limits.history_rows),
        ('object_refs','1',(),limits.reference_rows),
    )
    expressions,parameters = [],[]
    for table,predicate,values,limit in fields:
        expressions.append(f'(SELECT COUNT(*) FROM (SELECT 1 FROM {table} WHERE {predicate} LIMIT ?))')
        parameters.extend((*values,limit+1))
    cursor = connection.execute('SELECT '+','.join(expressions),tuple(parameters))
    try:
        row = tuple(cursor.fetchone())
    finally:
        cursor.close()
    for value,(_,_,_,limit) in zip(row,fields):
        _require(type(value) is int and 0 <= value <= limit,'existing_rows')
    return CheckpointFootprint(*row)
