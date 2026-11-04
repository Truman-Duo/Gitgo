"""Retain unconsumed configuration fields across versioned readers/writers.

Known fields and explicit record removals are controlled by the current model;
older or retired extensions remain stored without becoming active settings.
"""
from copy import deepcopy


def preserve_fields(before, after, *, replace_paths=()):
    """Retain unknown fields while allowing current owners to clear known maps.

    A '*' component denotes an identified record in a list, never a guessed
    project identity. Only explicitly declared owned values replace wholesale.
    """
    return _preserve(before, after, frozenset(replace_paths), ())


def _preserve(before, after, replace_paths, path):
    if path in replace_paths:
        return deepcopy(after)
    if type(before) is dict and type(after) is dict:
        result = deepcopy(before)
        for key, value in after.items():
            result[key] = _preserve(before.get(key), value, replace_paths, (*path, key))
        return result
    if type(before) is list and type(after) is list:
        identity = next((key for key in ("id", "project_id", "name") if
            all(type(item) is dict and key in item and type(item[key]) is str for item in before + after)
            and len({item[key] for item in before}) == len(before)
            and len({item[key] for item in after}) == len(after)), None)
        if identity:
            previous = {item[identity]: item for item in before}
            return [_preserve(previous.get(item[identity]), item, replace_paths, (*path, "*")) for item in after]
    return deepcopy(after)
