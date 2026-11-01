"""Retain unconsumed configuration fields across versioned readers/writers.

Known fields and explicit record removals are controlled by the current model;
older or retired extensions remain stored without becoming active settings.
"""
from copy import deepcopy


def preserve_fields(before, after):
    if type(before) is dict and type(after) is dict:
        result = deepcopy(before)
        for key, value in after.items():
            result[key] = preserve_fields(before.get(key), value)
        return result
    if type(before) is list and type(after) is list:
        identity = next((key for key in ("id", "project_id", "name") if
            all(type(item) is dict and key in item and type(item[key]) is str for item in before + after)
            and len({item[key] for item in before}) == len(before)
            and len({item[key] for item in after}) == len(after)), None)
        if identity:
            previous = {item[identity]: item for item in before}
            return [preserve_fields(previous.get(item[identity]), item) for item in after]
    return deepcopy(after)
