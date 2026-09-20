"""RFC-compatible finite JSON at every formal data boundary."""
import json as _json
from json import JSONDecodeError, JSONEncoder, JSONDecoder
import math


def _reject(value):
    raise JSONDecodeError("Non-finite JSON number is forbidden", "", 0)


def _float(value):
    result = float(value)
    return result if math.isfinite(result) else _reject(value)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise JSONDecodeError("Duplicate JSON key is forbidden", "", 0)
        result[key] = value
    return result


def loads(value, **kwargs):
    kwargs.update(parse_constant=_reject, parse_float=_float, object_pairs_hook=_pairs)
    return _json.loads(value, **kwargs)


def load(handle, **kwargs):
    return loads(handle.read(), **kwargs)


def dumps(value, **kwargs):
    kwargs["allow_nan"] = False
    return _json.dumps(value, **kwargs)


def dump(value, handle, **kwargs):
    handle.write(dumps(value, **kwargs))
