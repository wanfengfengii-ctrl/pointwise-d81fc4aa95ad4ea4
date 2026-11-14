"""Compatibility facade. The interpreter was split into exprkernel,
relationkernel and coordinator; this module only forwards the legacy entry."""
import coordinator


def run(query, tables, params=None):
    return coordinator.run(query, tables, params)
