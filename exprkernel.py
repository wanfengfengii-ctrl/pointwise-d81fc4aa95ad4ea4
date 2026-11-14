"""Expression kernel: name lookup, scalar operators, short-circuit, aggregates.

Owns every expression rule: column resolution, literal/param evaluation,
boolean and arithmetic operators, aggregate argument evaluation and the
subquery consumption rules (scalar/exists/in). It never receives tables,
never reads sources and never creates relation executors; subqueries are
consumed only through the request protocol below, routed by the coordinator.

evaluate(e, env, params, group=None, aggregating=False) returns a generator.
Start it with next, answer requests with send, inject business failures with
throw(protocol.Fault(message)); the scalar result is StopIteration.value.
env is a near-to-far list of frames; a frame is a list of (alias, names, row)
bindings. group is None or a list of member envs ([] means an empty group).

Requests emitted:
  ("trace", event)               -> None; subquery events keep their old spot.
  ("open", query, outer, flag)   -> opaque handle; only builds the relation
                                    generator, never advances it. The caller's
                                    group/aggregating context is not passed.
  ("next", handle)               -> ("row", row) or ("end",); failures arrive
                                    via throw, never as returned objects.
  ("close", handle)              -> None; every successful open is closed
                                    exactly once, on success, early hit and
                                    error alike. The handle is carried
                                    opaquely and never inspected.
"""
from protocol import Fault


def _column(env, ref):
    for frame in env:
        if len(ref) == 2:
            hits = [row[i] for alias, names, row in frame
                    for i, name in enumerate(names) if name == ref[1]]
        else:
            bindings = [binding for binding in frame if binding[0] == ref[1]]
            if not bindings:
                continue
            if len(bindings) != 1:
                raise Fault('AMBIGUOUS:' + '.'.join(ref[1:]))
            _, names, row = bindings[0]
            hits = [row[i] for i, name in enumerate(names) if name == ref[2]]
            if not hits:
                raise Fault('UNKNOWN:' + '.'.join(ref[1:]))
        if hits:
            if len(hits) != 1:
                raise Fault('AMBIGUOUS:' + '.'.join(ref[1:]))
            return hits[0]
    raise Fault('UNKNOWN:' + '.'.join(ref[1:]))


def _truth(value):
    if value is None:
        return None
    if type(value) is not bool:
        raise Fault('BOOL_REQUIRED')
    return value


def evaluate(e, env, params, group=None, aggregating=False):
    tag = e[0]
    if tag == 'lit':
        return e[1]
    if tag == 'col':
        return _column(env, e)
    if tag == 'param':
        if e[1] not in params:
            raise Fault('PARAM:' + e[1])
        return params[e[1]]
    if tag == 'if':
        condition = yield from evaluate(e[1], env, params, group, aggregating)
        branch = e[2] if _truth(condition) is True else e[3]
        return (yield from evaluate(branch, env, params, group, aggregating))
    if tag == 'coalesce':
        for part in e[1:]:
            value = yield from evaluate(part, env, params, group, aggregating)
            if value is not None:
                return value
        return None
    if tag == 'not':
        value = _truth((yield from evaluate(e[1], env, params, group, aggregating)))
        return None if value is None else not value
    if tag in ('scalar', 'exists', 'in'):
        # IN must evaluate the needle even for an empty right side.
        needle = None
        if tag == 'in':
            needle = yield from evaluate(e[1], env, params, group, aggregating)
        sub = e[2] if tag == 'in' else e[1]
        yield ('trace', ['subquery', tag])
        if tag != 'exists' and len(sub['select']) != 1:
            raise Fault('SUBQUERY_WIDTH')
        handle = yield ('open', sub, env, tag == 'exists')
        try:
            if tag == 'exists':
                delivered = yield ('next', handle)
                return delivered[0] == 'row'
            if tag == 'scalar':
                first = yield ('next', handle)
                if first[0] == 'end':
                    return None
                # The second row is projected before cardinality rejection.
                second = yield ('next', handle)
                if second[0] == 'row':
                    raise Fault('SCALAR_MANY')
                return first[1][0]
            unknown = False
            while True:
                delivered = yield ('next', handle)
                if delivered[0] == 'end':
                    break
                row = delivered[1]
                if needle is None or row[0] is None:
                    unknown = True
                elif needle == row[0]:
                    return True
            return None if unknown else False
        finally:
            yield ('close', handle)
    if tag == 'agg':
        if aggregating:
            raise Fault('NESTED_AGG')
        if group is None:
            raise Fault('NO_GROUP')
        op = e[1]
        values = []
        for member in group:
            values.append((yield from evaluate(e[2], member, params, None, True)))
        nonnull = [value for value in values if value is not None]
        if op == 'count':
            return len(nonnull)
        if op == 'sum':
            if any(type(value) is not int for value in nonnull):
                raise Fault('INT_REQUIRED')
            return sum(nonnull) if nonnull else None
        if op == 'min':
            try:
                return min(nonnull) if nonnull else None
            except TypeError:
                raise Fault('COMPARISON')
        raise Fault('AGG:' + op)
    if tag == 'op':
        op = e[1]
        left = yield from evaluate(e[2], env, params, group, aggregating)
        if op in ('and', 'or'):
            left = _truth(left)
            if op == 'and' and left is False:
                return False
            if op == 'or' and left is True:
                return True
            right = _truth((yield from evaluate(e[3], env, params, group, aggregating)))
            if op == 'and':
                return False if right is False else (None if left is None or right is None else True)
            return True if right is True else (None if left is None or right is None else False)
        right = yield from evaluate(e[3], env, params, group, aggregating)
        if left is None or right is None:
            return None
        if op == 'eq':
            return left == right
        if op in ('add', 'div'):
            if type(left) is not int or type(right) is not int:
                raise Fault('INT_REQUIRED')
            if op == 'div' and right == 0:
                raise Fault('DIV_ZERO')
            return left + right if op == 'add' else left // right
        if op == 'lt':
            try:
                return left < right
            except TypeError:
                raise Fault('COMPARISON')
        raise Fault('OP:' + op)
    raise Fault('EXPR:' + tag)
