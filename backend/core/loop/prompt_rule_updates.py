"""Exact compiler-section changes derived from visible authoritative history.

No new policy store: records retain source/version identities and reconstruct
only compiler text. They neither summarize semantics nor change its authority.
"""
from difflib import SequenceMatcher
import json

from .provider_protocol import deterministic_hash

FORMAT = 'gitgo.prompt.sections.v1'
HEADER = (
    '[HOST RULE SECTION UPDATES]\n'
    'Apply records in order to the named compiler sections only; their source authority is unchanged. '
    'replace supplies the entire current section; remove withdraws it. '
    'patch retains all unchanged text and replaces only the listed spans of its base section. '
    'Each edit includes old_text, insert and nearby unchanged context so the change can be read directly without counting characters. '
    'Offsets and delete counts are Unicode character counts, zero-based, applied in descending offset order. '
    'Removed text no longer applies. Later section versions supersede earlier ones. '
    'These are rule updates, not user requests or permission grants.\n'
)


def identity(value):
    return deterministic_hash({key: value[key] for key in ('name', 'provenance', 'version', 'content')})


def record(section):
    return {key: getattr(section, key) for key in ('name', 'provenance', 'version', 'content')}


def render_sections(items):
    text, metadata = '', []
    for item in items:
        text += ('\n\n' if text else '') + '## ' + item['name'] + '\n'
        start = len(text)
        text += item['content']
        metadata.append({key: item[key] for key in ('name', 'provenance', 'version')} | {
            'content_range': [start, len(text)], 'section_hash': identity(item),
            'estimated_tokens': max(1, len(item['content']) // 4)})
    return text, metadata


def _seed_sections(message):
    text = message.get('content', '')
    metadata = message.get('prompt_sections') or []
    for index, item in enumerate(metadata):
        try:
            if 'content_range' in item:
                start, end = item['content_range']
            else:
                # Legacy prefixes are recognized by their own section metadata,
                # not by interpreting arbitrary quoted headings as authority.
                marker = '## ' + item['name'] + '\n'
                start = text.index(marker) + len(marker)
                end = (text.index('\n\n## ' + metadata[index + 1]['name'] + '\n', start)
                       if index + 1 < len(metadata) else len(text))
            if not 0 <= start <= end <= len(text):
                continue
            value = {'name': item['name'], 'provenance': item['provenance'],
                     'version': item.get('version', '1'), 'content': text[start:end]}
            if item.get('section_hash', identity(value)) == identity(value):
                yield value
        except (KeyError, TypeError, ValueError):
            continue


def apply_update(current, update):
    if update['operation'] == 'replace':
        value = {key: update[key] for key in ('name', 'provenance', 'version', 'content')}
    elif update['operation'] == 'patch':
        if current is None or identity(current) != update['base_hash']:
            raise ValueError('Unknown rule section base')
        content = current['content']
        previous_start = len(content) + 1
        for edit in update['edits']:
            start, count = edit['offset'], edit['delete']
            if not 0 <= start <= start + count <= len(content) or start + count > previous_start:
                raise ValueError('Invalid or overlapping rule edit')
            if content[start:start + count] != edit['old_text']:
                raise ValueError('Rule edit text mismatch')
            content = content[:start] + edit['insert'] + content[start + count:]
            previous_start = start
        value = {key: update[key] for key in ('name', 'provenance', 'version')} | {'content': content}
    else:
        raise ValueError('Unknown rule operation')
    if identity(value) != update['section_hash']:
        raise ValueError('Rule section digest mismatch')
    return value


def visible_sections(messages):
    """Read genuine compiler prefixes/deltas only; quoted text never counts."""
    current = {}
    for message in messages:
        if message.get('provider_visible') is False:
            continue
        if (message.get('role') == 'system' and message.get('message_type') in
                {'compiled_system_prompt', 'compiled_task_contract'}):
            current.update({value['name']: value for value in _seed_sections(message)})
        elif (message.get('host_authority') is True and message.get('message_type') == 'host_steering_delta'
              and message.get('steering_type') == 'prompt_contract_update'):
            content = message.get('content') or ''
            if not content.startswith(HEADER):
                # Old prose updates cannot safely supply an exact patch base.
                # The next compiler refresh supplies a current full replacement.
                current.clear()
                continue
            try:
                payload = json.loads(content[len(HEADER):])
                if payload['format'] != FORMAT:
                    raise ValueError('Unknown rule update format')
                for update in payload['updates']:
                    name = update['name']
                    if update['operation'] == 'remove':
                        current.pop(name, None)
                    else:
                        try:
                            current[name] = apply_update(current.get(name), update)
                        except (KeyError, TypeError, ValueError):
                            current.pop(name, None)
            except (KeyError, TypeError, ValueError):
                current.clear()
    return current


def _splice(before, after, offset):
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    end = 0
    while end < min(len(before), len(after)) - start and before[-end - 1] == after[-end - 1]:
        end += 1
    finish = len(before) - end
    return {'offset': offset + start, 'delete': finish - start,
            'old_text': before[start:finish],
            'context_before': before[max(0, start - 100):start],
            'context_after': before[finish:finish + 100],
            'insert': after[start:len(after) - end if end else len(after)]}


def section_update(previous, value):
    full = {'operation': 'replace', **value, 'section_hash': identity(value)}
    if previous is None:
        return full
    before, after = previous['content'], value['content']
    old_lines, new_lines = before.splitlines(keepends=True), after.splitlines(keepends=True)
    # Bound diff work for huge generated sections; the linear splice fallback
    # remains lossless. Rules normally have tens of lines, not file-sized text.
    if len(old_lines) + len(new_lines) > 2048:
        edits = [_splice(before, after, 0)]
    else:
        offsets = [0]
        for line in old_lines:
            offsets.append(offsets[-1] + len(line))
        edits = []
        for kind, i, j, k, l in SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
            if kind == 'equal':
                continue
            edit = _splice(''.join(old_lines[i:j]), ''.join(new_lines[k:l]), offsets[i])
            start, finish = edit['offset'], edit['offset'] + edit['delete']
            edit.update(context_before=before[max(0, start - 100):start], context_after=before[finish:finish + 100])
            edits.append(edit)
    patch = {'operation': 'patch', **{key: value[key] for key in ('name', 'provenance', 'version')},
             'base_hash': identity(previous), 'section_hash': identity(value),
             'edits': list(reversed(edits))}
    # Do not pay a patch protocol overhead larger than a full replacement.
    return min((patch, full), key=lambda item: len(json.dumps(item, ensure_ascii=False)))


def update_text(updates):
    return HEADER + json.dumps({'format': FORMAT, 'updates': updates},
                               ensure_ascii=False, separators=(',', ':'))


def compact_prefix(session):
    """Materialize one current rule baseline only at an existing epoch break."""
    from .prompt_compiler import STABLE_SECTION_NAMES, PROMPT_SCHEMA_VERSION
    prefix = [item for item in session.messages if item.get('message_type') in {
        'compiled_system_prompt', 'compiled_task_contract'}][:2]
    expected = {name: digest for name, digest in session.compiled_prompt_sections.items()
                if name != 'Project governance context'}
    if not expected:
        return prefix
    current = visible_sections(session.messages)
    if set(current) != set(expected) or any(identity(current[name]) != digest for name, digest in expected.items()):
        # Never drop an unknown legacy/corrupt update just to claim compaction.
        return prefix + [item for item in session.messages if item.get('host_authority') is True
                         and item.get('steering_type') == 'prompt_contract_update']
    result = []
    for stable, kind in ((True, 'compiled_system_prompt'), (False, 'compiled_task_contract')):
        items = [value for name, value in current.items() if (name in STABLE_SECTION_NAMES) == stable]
        content, metadata = render_sections(items)
        result.append({'role': 'system', 'content': content, 'message_type': kind,
                       'prompt_sections': metadata, 'prompt_schema_version': PROMPT_SCHEMA_VERSION,
                       'prompt_hash': '', 'context_epoch': session.context_epoch})
    return result
