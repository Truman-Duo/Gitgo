"""Host-owned public answer fragments across provider truncations/interruptions.

Provider turns stay separate for protocol replay. Only public text from an
incomplete turn without tool calls belongs to the eventual answer; reasoning
and ordinary pre-tool commentary never become delivery fragments.
"""
from __future__ import annotations

KEY = 'provider_answer_continuation'


def pending_answer_text(process):
    context, _ = process.read_context_snapshot()
    state = context.get(KEY) or {}
    if (state.get('task_id') != (process.active_task_id or process.process_id)
            or state.get('process_id') != process.process_id):
        return ''
    return ''.join(state.get('parts') or [])


def remember_answer_fragment(process, text):
    if not text:
        return
    prefix = pending_answer_text(process)
    state = {'task_id': process.active_task_id or process.process_id,
             'process_id': process.process_id, 'parts': [prefix, text] if prefix else [text]}
    process.update_context_snapshot(lambda ctx: {**ctx, KEY: state})


def clear_answer_fragments(process):
    process.update_context_snapshot(lambda ctx: {k: v for k, v in ctx.items() if k != KEY})
