"""Relation kernel: sources, lateral enumeration, grouping and row shaping.

Owns table scans, lateral derived sources, outer-join null filling, grouping,
projection order, distinct, sorting and limit. It never evaluates any
expression, not even literals, column references or aggregates: every value
comes from an eval request answered by the coordinator. The boolean type
check of where/having replies and the filtering decision stay here.

execute(q, outer, tables, existence=False) returns a generator speaking the
request protocol:
  ("eval", e, env, group, aggregating) -> the expression scalar; failures
                                    arrive via throw. A fresh query starts
                                    with no group context; where, group keys
                                    and plain projection keep the legacy
                                    arguments, having/aggregate projection
                                    carries the local group.
  ("trace", event)                -> None; scan events keep their old spot.
  ("row", row)                    -> delivers one final result row; resumed
                                    with send(None) only when the next row
                                    is pulled. Returning None means the end;
                                    rows of internal lateral sources are
                                    never delivered as outer results.

The kernel may recurse into itself for lateral sources, but every expression
still goes through an eval request.
"""
import json
from protocol import Fault


def _key(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _truth(value):
    if value is None:
        return None
    if type(value) is not bool:
        raise Fault('BOOL_REQUIRED')
    return value


def _forward(gen, kinds):
    """Pump a nested producer until it yields an item of a kind in kinds or
    ends. Other requests travel upward and their replies feed back down."""
    response = None
    while True:
        try:
            request = gen.send(response)
        except StopIteration:
            return None
        if request[0] in kinds:
            return request
        response = yield request


def execute(q, outer, tables, existence=False):
    if q.get('limit') == 0:
        return

    def source(index, frame):
        if index == len(q.get('from', [])):
            env = [frame] + outer
            where = yield ('eval', q.get('where', ['lit', True]), env, None, False)
            if _truth(where) is True:
                yield ('emit', env)
            return
        item = q['from'][index]
        if 'table' in item:
            name = item['table']
            yield ('trace', ['scan', name])
            if name not in tables:
                raise Fault('TABLE:' + name)
            names = tables[name]['columns']
            seen = False
            for row in tables[name]['rows']:
                seen = True
                yield from source(index + 1, frame + [(item['as'], names, row)])
            if not seen and item.get('left', False):
                binding = (item['as'], names, [None] * len(names))
                yield from source(index + 1, frame + [binding])
            return
        names = [pair[0] for pair in item['query']['select']]
        # A lateral relation sees the current prefix, including an empty frame.
        child = execute(item['query'], [frame] + outer, tables)
        seen = False
        while True:
            delivered = yield from _forward(child, ('row',))
            if delivered is None:
                break
            seen = True
            binding = (item['as'], names, delivered[1])
            yield from source(index + 1, frame + [binding])
        if not seen and item.get('left', False):
            binding = (item['as'], names, [None] * len(names))
            yield from source(index + 1, frame + [binding])

    def candidates():
        if 'group' not in q:
            producer = source(0, [])
            while True:
                item = yield from _forward(producer, ('emit',))
                if item is None:
                    return
                yield ('emit', (item[1], None))
        groups = {}
        producer = source(0, [])
        while True:
            item = yield from _forward(producer, ('emit',))
            if item is None:
                break
            env = item[1]
            values = []
            for e in q['group']:
                values.append((yield ('eval', e, env, None, False)))
            groups.setdefault(_key(values), []).append(env)
        if not q['group'] and not groups:
            groups['[]'] = []
        for members in groups.values():
            if members:
                env = members[0]
            else:
                # No physical source is scanned here; only declared layouts matter.
                frame = []
                for item in q.get('from', []):
                    if 'table' in item:
                        name = item['table']
                        if name not in tables:
                            raise Fault('TABLE:' + name)
                        names = tables[name]['columns']
                    else:
                        names = [pair[0] for pair in item['query']['select']]
                    frame.append((item['as'], names, [None] * len(names)))
                env = [frame] + outer
            having = yield ('eval', q.get('having', ['lit', True]), env, members, False)
            if _truth(having) is True:
                yield ('emit', (env, members))

    def projected():
        producer = candidates()
        while True:
            item = yield from _forward(producer, ('emit',))
            if item is None:
                return
            env, group = item[1]
            if existence:
                yield ('emit', [])
            else:
                row = []
                for _, e in q['select']:
                    row.append((yield ('eval', e, env, group, False)))
                yield ('emit', row)

    def unique(producer):
        seen = set()
        while True:
            item = yield from _forward(producer, ('emit',))
            if item is None:
                return
            row = item[1]
            encoded = _key(row)
            if encoded not in seen:
                seen.add(encoded)
                yield ('emit', row)

    # EXISTS ignores projection, DISTINCT and ORDER; LIMIT=0 still suppresses it.
    producer = projected()
    if not existence and q.get('distinct', False):
        producer = unique(producer)
    if not existence and q.get('order'):
        materialized = []
        while True:
            item = yield from _forward(producer, ('emit',))
            if item is None:
                break
            materialized.append(item[1])
        for index, descending in reversed(q['order']):
            def sort_key(row, index=index):
                value = row[index]
                return (value is not None, type(value).__name__, value)
            materialized.sort(key=sort_key, reverse=descending)

        def sorted_rows():
            for row in materialized:
                yield ('emit', row)
        producer = sorted_rows()
    limit = q.get('limit')
    count = 0
    while limit is None or count < limit:
        item = yield from _forward(producer, ('emit',))
        if item is None:
            return
        yield ('row', item[1])
        count += 1
