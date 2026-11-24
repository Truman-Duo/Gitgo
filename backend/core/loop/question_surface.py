"""Small model-facing question inputs over the existing decision/workflow service.

Models choose the question, recommendation and semantic dependencies. Runtime
assembles identities; this compiler neither answers nor grants anything.
"""
from copy import deepcopy
import hashlib
import json


class QuestionInputError(ValueError):
    code = 'QUESTION_ANSWER_KIND_INVALID'

    def __init__(self, value, field_path):
        self.details = {'field_path': field_path, 'allowed': ['choice', 'evidence'],
                        'received': value if isinstance(value, str) else type(value).__name__}
        super().__init__(field_path + ': use choice for a user preference or evidence for an unknown fact; '
                         'omit the optional field if unclassified. No question was published.')


class QuestionOptionCountError(QuestionInputError):
    code = 'QUESTION_OPTION_COUNT_INVALID'

    def __init__(self, count, field_path, allowed):
        self.details = {'field_path': field_path, 'received_count': count, 'allowed_counts': list(allowed)}
        ValueError.__init__(self, field_path + ': allowed option counts are ' + ', '.join(map(str, allowed)) + '. '
            'The UI already provides free-form discussion/amendment; do not add a defer option. '
            'No question or accompanying answer update was published.')


class QuestionAuthorityOptionError(QuestionInputError):
    code = 'QUESTION_AUTHORITY_OPTION_INVALID'

    def __init__(self, field_path):
        self.details = {'field_path': field_path, 'authority_granted': False}
        ValueError.__init__(self, field_path + ': a business answer option cannot contain an authority action. '
            'Describe the user choice without runtime actions; no question or answer update was published.')


def validate_question_kinds(request):
    """Validate small protocol fields before binding any accompanying reply."""
    if not isinstance(request, dict):
        return  # Existing shape validation owns other errors.
    items = request.get('questions')
    if items is None:
        items = [request]
    if not isinstance(items, list):
        return
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        value = item.get('answer_kind')
        if value is not None and (not isinstance(value, str) or value not in ('choice', 'evidence')):
            path = f'/questions/{index}/answer_kind' if 'questions' in request else '/answer_kind'
            raise QuestionInputError(value, path)
        options = item.get('options')
        kind = item.get('kind', request.get('kind', 'clarification'))
        if isinstance(options, list):
            for option_index, option in enumerate(options):
                if isinstance(option, dict) and option.get('action') and kind in {'clarification', 'runtime_checkpoint'}:
                    path = f'/questions/{index}/options/{option_index}/action' if 'questions' in request else f'/options/{option_index}/action'
                    raise QuestionAuthorityOptionError(path)
        from .decision_support import decision_option_counts
        allowed = decision_option_counts(kind)
        if isinstance(options, list) and len(options) not in allowed:
            path = f'/questions/{index}/options' if 'questions' in request else '/options'
            raise QuestionOptionCountError(len(options), path, allowed)


def question_schema():
    fields = {
        'question': {'type': 'string'},
        'why_user_must_decide': {'type': 'string'},
        'recommendation': {'type': 'string'},
        'answer_kind': {'type': 'string', 'enum': ['choice', 'evidence'],
                        'description': 'Optional: choice=user preference; evidence=unknown fact. Omit if unclassified.'},
        'options': {'type': 'array', 'maxItems': 3, 'items': {'type': 'object',
            'properties': {'label': {'type': 'string'}, 'description': {'type': 'string'},
                           'recommended': {'type': 'boolean'}}, 'required': ['label']}},
        'state_topic': {'type': 'string'},
        'depends_on_topics': {'type': 'array', 'items': {'type': 'string'}},
        'supersedes_decision_id': {'type': 'string'},
    }
    required = ['question', 'why_user_must_decide', 'recommendation']
    return {'type': 'object', 'properties': {**fields,
        'answer_updates': {'type': 'array', 'maxItems': 16, 'items': {'type': 'object'}},
        'questions': {'type': 'array', 'minItems': 1, 'maxItems': 8,
            'items': {'type': 'object', 'properties': fields, 'required': required}}},
        'anyOf': [{'required': required}, {'required': ['questions']}]}


def question_proposals(process, requests, *, require_recommendation=False):
    """Expand one compact call or old single calls without changing authority.

    Legacy callers may omit the new recommendation. Keep their input usable;
    the current model schema requires one and semantic review can supply it.
    Never invent a preferred answer by selecting the first option.
    """
    from .decision_support import prepare_user_decision, SEMANTIC_DECISION_KINDS
    result = []
    for request in requests:
        validate_question_kinds(request)
        if not isinstance(request, dict):
            raise ValueError('question input must be an object')
        items = request.get('questions')
        if items is None:
            items = [request]
        elif (not isinstance(items, list) or not 1 <= len(items) <= 8
              or request.get('kind', 'clarification') != 'clarification'):
            raise ValueError('an independent question group requires 1..8 clarification questions')
        for source in items:
            if not isinstance(source, dict):
                raise ValueError('each question must be an object')
            item = deepcopy(source)
            item.pop('answer_updates', None)  # interpretation is not question/card content
            for field, limit in [('question', 4000), ('why_user_must_decide', 2000)]:
                if not isinstance(item.get(field), str) or not item[field].strip() or len(item[field]) > limit:
                    raise ValueError(field + ' must be nonempty bounded text')
            item.setdefault('kind', 'clarification')
            item['options'] = question_options(item)
            if item['kind'] not in SEMANTIC_DECISION_KINDS:
                raise ValueError('business questions cannot request authority or runtime control')
            recommendation = item.get('recommendation', '')
            if not isinstance(recommendation, str) or len(recommendation) > 4000:
                raise ValueError('recommendation must be at most 4000 characters')
            if not recommendation.strip():
                selected = [option for option in item['options']
                            if isinstance(option, dict) and option.get('recommended')]
                recommendation = selected[0].get('label', '') if len(selected) == 1 else ''
            if recommendation.strip():
                item['recommendation'] = recommendation.strip()
            topic = str(item.get('state_topic') or '').strip()
            item['state_topic'] = topic or item['kind'] + ':' + hashlib.sha256(
                str(item.get('question') or '').encode()).hexdigest()[:16]
            deps = item.get('depends_on_topics', [])
            if not isinstance(deps, list) or any(not isinstance(dep, str) or not dep.strip() for dep in deps):
                raise ValueError('depends_on_topics requires exact semantic topics')
            # Validate before any paid review or publication. The temporary
            # validation identity is not an input the model needs to generate.
            prepare_user_decision(process, item)
            if require_recommendation and not recommendation.strip():
                raise ValueError('include a recommendation for each question; do not invent a user answer')
            result.append(item)
    if not 1 <= len(result) <= 8 or len(json.dumps(result, ensure_ascii=False).encode()) > 32000:
        raise ValueError('propose 1..8 bounded questions')
    topics = [item['state_topic'] for item in result]
    if len(set(topics)) != len(topics):
        raise ValueError('propose each semantic topic once')
    return result


def question_options(source):
    """A recommendation may suggest a check, never favor an unknown fact.

    The Agent supplies meaning through answer_kind. Unclassified modern input
    keeps its advice text but conservatively has no preferred factual label.
    Old full decision inputs without the new advice field remain compatible.
    """
    kind = source.get('answer_kind')
    validate_question_kinds(source)
    options = deepcopy(source.get('options') or [])
    if kind != 'choice' and ('recommendation' in source or kind is not None):
        for option in options:
            if isinstance(option, dict):
                option.pop('recommended', None)
    return options
