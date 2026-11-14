"""Coordinator: routes requests between the expression and relation kernels.

Owns every live generator, the opaque relation handles, request routing,
business-fault injection and trace collection. It interprets no query keys,
expression tags, operators or row values; the only query key it reads is the
top-level select list, to assemble the result columns. Kernels are reached
through the module attributes exprkernel.evaluate and relationkernel.execute,
so either kernel can be substituted without changing the routes.
"""
import exprkernel
import relationkernel
from protocol import Fault


def run(query, tables, params=None):
    trace = []
    params = {} if params is None else params
    handles = {}
    owners = []

    def close_handle(handle):
        # Closing never advances reads and adds no business trace; unfinished
        # descendants of the relation are closed with it.
        entry = handles.pop(handle, None)
        if entry is None:
            return
        for child in list(entry['children']):
            close_handle(child)
        if entry['parent'] is not None:
            entry['parent']['children'].discard(handle)
        entry['gen'].close()

    def drive_expression(e, env, group, aggregating):
        gen = exprkernel.evaluate(e, env, params, group, aggregating)
        response = None
        error = None
        while True:
            try:
                if error is not None:
                    request = gen.throw(error)
                else:
                    request = gen.send(response)
            except StopIteration as stop:
                return stop.value
            response = None
            error = None
            kind = request[0]
            if kind == 'trace':
                trace.append(request[1])
            elif kind == 'open':
                handle = object()
                entry = {'gen': relationkernel.execute(request[1], request[2],
                                                       tables, request[3]),
                         'children': set(),
                         'parent': owners[-1] if owners else None}
                handles[handle] = entry
                if entry['parent'] is not None:
                    entry['parent']['children'].add(handle)
                response = handle
            elif kind == 'next':
                try:
                    response = pull_entry(handles[request[1]])
                except Fault as exc:
                    error = exc
            elif kind == 'close':
                close_handle(request[1])

    def resume(gen, response, error):
        try:
            if error is not None:
                return gen.throw(error)
            return gen.send(response)
        except StopIteration:
            return None

    def service(gen, request):
        if request[0] == 'trace':
            trace.append(request[1])
            return resume(gen, None, None)
        _, e, env, group, aggregating = request
        try:
            value = drive_expression(e, env, group, aggregating)
        except Fault as exc:
            return resume(gen, None, exc)
        return resume(gen, value, None)

    def pull_entry(entry):
        gen = entry['gen']
        owners.append(entry)
        try:
            request = resume(gen, None, None)
            while request is not None and request[0] != 'row':
                request = service(gen, request)
        finally:
            owners.pop()
        if request is None:
            return ('end',)
        return ('row', request[1])

    top = {'gen': relationkernel.execute(query, [], tables, False),
           'children': set(), 'parent': None}
    rows = []
    try:
        while True:
            delivered = pull_entry(top)
            if delivered[0] == 'end':
                break
            rows.append(delivered[1])
    except Fault as exc:
        return {'error': str(exc), 'trace': trace}
    return {'columns': [pair[0] for pair in query['select']],
            'rows': rows, 'trace': trace}
