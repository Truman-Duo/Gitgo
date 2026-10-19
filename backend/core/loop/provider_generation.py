"""Generation preferences by workload, independent of task semantics.

Responses defines an optional reasoning effort parameter. Try its conservative
low setting for isolated alignment and knowledge summaries; an explicit parameter rejection may
negotiate back to the provider default once. Never infer support from model names.
"""
import json


def reasoning_effort(protocol, purpose, unsupported=False):
    return 'low' if (str(getattr(protocol, 'value', protocol)) == 'openai_responses'
                     and purpose in {'alignment', 'knowledge_harvest'} and not unsupported) else None


def rejects_reasoning_preference(error):
    from .provider_transport import ProviderHttpError
    if not isinstance(error, ProviderHttpError) or error.status_code != 400:
        return False
    try:
        data = json.loads(error.body)
    except (TypeError, ValueError):
        return False
    detail = data.get('error') if isinstance(data, dict) else None
    if not isinstance(detail, dict):
        return False
    parameter = str(detail.get('param') or '')
    code = str(detail.get('code') or '')
    message = str(detail.get('message') or '').casefold()
    return (parameter in {'reasoning', 'reasoning.effort', 'reasoning_effort'}
            and (code in {'unsupported_parameter', 'unsupported_value', 'unknown_parameter'}
                 or any(term in message for term in ('not supported', 'unsupported', 'unknown parameter'))))
