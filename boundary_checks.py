"""Public target probes. These test doubles contain no query evaluator."""
import ast
import importlib
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from protocol import Fault
from samples import TABLES, TOTALS, shared, recursive


def graph_stamp(root):
    seen, nodes = {}, []

    def visit(value):
        if not isinstance(value, (dict, list, tuple)):
            return ['value', value]
        if id(value) in seen:
            return ['ref', seen[id(value)]]
        index = len(nodes)
        seen[id(value)] = index
        nodes.append(None)
        if isinstance(value, dict):
            body = ['dict', [[k, visit(v)] for k, v in sorted(value.items())]]
        else:
            body = [type(value).__name__, [visit(v) for v in value]]
        nodes[index] = body
        return ['ref', index]
    visit(root)
    return nodes


def finish(program, steps, value=None, error=None):
    """A fixed script, never a fallback implementation of either kernel."""
    reply, injected = None, False
    for expected, response in steps:
        request = program.throw(reply) if injected else program.send(reply)
        assert request == expected, (request, expected)
        reply, injected = response, isinstance(response, Fault)
    try:
        extra = program.throw(reply) if injected else program.send(reply)
    except StopIteration as stop:
        assert error is None, 'expected business fault: ' + str(error)
        assert stop.value == value, (stop.value, value)
    except Fault as exc:
        assert error == str(exc), (str(exc), error)
    else:
        raise AssertionError(('unexpected request', extra))


def expression_probes(expr):
    env = [[('r', ['v'], [8])]]
    sub = {'select': [['v', ['lit', 1]]]}
    handle = object()
    opening = lambda kind: [
        (('trace', ['subquery', kind]), None),
        (('open', sub, env, kind == 'exists'), handle),
    ]
    closing = [(('close', handle), None)]
    finish(expr.evaluate(['lit', 9], env, {}), [], 9)
    bad = ['op', 'div', ['lit', 1], ['lit', 0]]
    finish(expr.evaluate(['op', 'and', ['lit', False], bad], env, {}), [], False)
    finish(expr.evaluate(['in', ['param', 'missing'], sub], env, {}), [], error='PARAM:missing')
    finish(expr.evaluate(['exists', sub], env, {}),
           opening('exists') + [(('next', handle), ('row', []))] + closing, True)
    finish(expr.evaluate(['in', ['lit', 4], sub], env, {}),
           opening('in') + [(('next', handle), ('row', [None])),
                            (('next', handle), ('row', [4]))] + closing, True)
    finish(expr.evaluate(['scalar', sub], env, {}),
           opening('scalar') + [(('next', handle), ('end',))] + closing, None)
    finish(expr.evaluate(['scalar', sub], env, {}),
           opening('scalar') + [(('next', handle), ('row', [1])),
                                (('next', handle), Fault('DIV_ZERO'))] + closing,
           error='DIV_ZERO')
    finish(expr.evaluate(['scalar', sub], env, {}),
           opening('scalar') + [(('next', handle), ('row', [1])),
                                (('next', handle), ('row', [2]))] + closing,
           error='SCALAR_MANY')
    group = [[[('r', ['v'], [3])]], [[('r', ['v'], [5])]]]
    finish(expr.evaluate(['agg', 'sum', ['col', 'r', 'v']], env, {}, group), [], 8)


def relation_probes(rel):
    expression = ['param', 'not_provided']
    q = {'from': [{'table': 't', 'as': 'r'}], 'select': [['v', expression]]}
    tables = {'t': {'columns': ['v'], 'rows': [[3], [5]]}}
    p = rel.execute(q, [], tables)
    assert next(p) == ('trace', ['scan', 't'])
    for number in (3, 5):
        request = p.send(None)
        assert request == ('eval', ['lit', True], [[('r', ['v'], [number])]], None, False)
        request = p.send(True)
        assert request == ('eval', expression, [[('r', ['v'], [number])]], None, False)
        assert p.send(77) == ('row', [77]), 'relation evaluated expression instead of using reply'
    finish(p, [], None)
    p = rel.execute(q, [], tables)
    assert next(p) == ('trace', ['scan', 't'])
    request = p.send(None)
    assert request[0] == 'eval'
    finish_after_fault(p)
    # Empty aggregate keeps a local empty group and synthetic null binding.
    q['group'] = []
    tables['t']['rows'] = []
    p = rel.execute(q, [], tables)
    assert next(p) == ('trace', ['scan', 't'])
    env = [[('r', ['v'], [None])]]
    assert p.send(None) == ('eval', ['lit', True], env, [], False)
    assert p.send(True) == ('eval', expression, env, [], False)
    assert p.send(91) == ('row', [91])
    finish(p, [], None)
    q['limit'] = 0
    finish(rel.execute(q, [], {}), [], None)


def finish_after_fault(program):
    try:
        program.throw(Fault('SCRIPTED'))
    except Fault as exc:
        assert str(exc) == 'SCRIPTED'
    else:
        raise AssertionError('relation swallowed expression fault')


def isolated_imports():
    for module, blocked, body in [
        ('exprkernel', ['relationkernel', 'coordinator', 'queryflow', 'samples'],
         "p=m.evaluate(['lit',6],[],{});\ntry: next(p)\nexcept StopIteration as s: assert s.value==6\nelse: raise AssertionError('literal requested execution')"),
        ('relationkernel', ['exprkernel', 'coordinator', 'queryflow', 'samples'],
         "p=m.execute({'select':[['v',['lit',6]]]},[],{}); assert next(p)[0]=='eval'; assert p.send(True)[0]=='eval'; assert p.send(23)==('row',[23])"),
    ]:
        script = (
            'import importlib, importlib.abc, sys\n'
            'class Block(importlib.abc.MetaPathFinder):\n'
            ' def find_spec(self, fullname, path=None, target=None):\n'
            '  if fullname.split(".")[0] in ' + repr(blocked) + ': raise AssertionError(fullname)\n'
            'sys.meta_path.insert(0, Block())\n'
            'm=importlib.import_module(' + repr(module) + ')\n' + body)
        subprocess.run([sys.executable, '-B', '-c', script], check=True)


def coordinator_probes(coord, expr, rel):
    q = {'select': [['v', ['param', 'absent']]]}
    events = []
    replies = iter([True, 81])

    def fake_expression(e, env, params, group=None, aggregating=False):
        events.append('expression')
        yield ('trace', ['scripted', 'expression'])
        return next(replies)

    with patch.object(expr, 'evaluate', fake_expression):
        assert coord.run(q, {}) == {
            'columns': ['v'], 'rows': [[81]], 'trace': [['scripted', 'expression']] * 2}
        assert events == ['expression', 'expression']

    def fake_relation(q, outer, tables, existence=False):
        yield ('trace', ['scripted', 'relation'])
        value = yield ('eval', ['lit', 37], outer, None, False)
        yield ('row', [value])

    with patch.object(rel, 'execute', fake_relation):
        assert coord.run(q, {}) == {
            'columns': ['v'], 'rows': [[37]], 'trace': [['scripted', 'relation']]}


def business_compatibility():
    from queryflow import run
    q, tables, params = shared(8)
    expected = [event for i in reversed(range(8)) for event in (
        ['subquery', 'scalar'], ['scan', 'one' if params['p' + str(i)] else 'two'],
        ['subquery', 'scalar'])]
    assert run(q, tables, params) == {'columns': ['v'], 'rows': [[12]], 'trace': expected}
    bad = ['op', 'div', ['lit', 1], ['lit', 0]]
    table = {'t': {'columns': ['v'], 'rows': [[4], [0]]}}
    sub = {'from': [{'table': 't', 'as': 'r'}], 'select': [['v',
        ['if', ['op', 'eq', ['col', 'r', 'v'], ['lit', 4]], ['lit', 4], bad]]]}
    q = {'select': [['v', ['in', ['lit', 4], sub]]]}
    assert run(q, table) == {'columns': ['v'], 'rows': [[True]],
                             'trace': [['subquery', 'in'], ['scan', 't']]}
    q = {'select': [['v', ['if', ['lit', True], ['lit', 9],
                         ['scalar', {'from': [{'table': 'absent', 'as': 'r'}],
                                     'select': [['v', bad]]}]]]]}
    assert run(q, {}) == {'columns': ['v'], 'rows': [[9]], 'trace': []}
    for q, tables, params in [(TOTALS, TABLES, {}), shared(8), recursive()]:
        before = graph_stamp([q, tables, params])
        result = run(q, tables, params)
        assert run(q, tables, params) == result
        assert before == graph_stamp([q, tables, params]), 'input graph changed'
    print('ADDITIONAL_BUSINESS_OK')


def regression():
    tree = ast.parse(Path('queryflow.py').read_text(encoding='utf-8'))
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert len(functions) == 1 and functions[0].name == 'run', 'facade retained execution helpers'
    assert not any(isinstance(n, ast.ClassDef) for n in ast.walk(tree))
    isolated_imports()
    expr = importlib.import_module('exprkernel')
    rel = importlib.import_module('relationkernel')
    coord = importlib.import_module('coordinator')
    expression_probes(expr)
    relation_probes(rel)
    coordinator_probes(coord, expr, rel)
    business_compatibility()
    print('BOUNDARY_REGRESSION_OK')


if __name__ == '__main__':
    if sys.argv[1:] == ['business']:
        business_compatibility()
    else:
        regression()
