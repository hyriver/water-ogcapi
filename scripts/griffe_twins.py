"""Griffe extension that documents each blocking twin in ``water_ogcapi._sync`` as a method.

Static analysis reads ``collect = _blocking(_client.OGCService.collect)`` as an
attribute, so the API page would list none of the blocking classes' query methods
(D-46, D-48). This replaces each such attribute with a copy of the async method it
wraps: same parameters and docstring, not async, and for ``_iterating`` an
``Iterator`` return in place of ``AsyncIterator``, as ``_iterating`` sets at runtime.
"""

from __future__ import annotations

from typing import Any

import griffe

WRAPPERS = frozenset({"_blocking", "_iterating"})


def _twin(name: str, wrapper: str, target: griffe.Function) -> griffe.Function:
    # Fresh copies: a Function takes over the parameters and docstring it is given.
    parameters = griffe.Parameters(
        *(
            griffe.Parameter(p.name, annotation=p.annotation, kind=p.kind, default=p.default)
            for p in target.parameters
        )
    )
    docstring = None
    if target.docstring is not None:
        docstring = griffe.Docstring(
            target.docstring.value,
            lineno=target.docstring.lineno,
            endlineno=target.docstring.endlineno,
            parser=target.docstring.parser,
            parser_options=target.docstring.parser_options,
        )
    returns = target.returns
    if wrapper == "_iterating":
        returns = str(returns).removeprefix("Async")
    return griffe.Function(
        name,
        parameters=parameters,
        returns=returns,
        docstring=docstring,
        lineno=target.lineno,
        endlineno=target.endlineno,
    )


class BlockingTwins(griffe.Extension):
    """Turn every ``_blocking(...)`` and ``_iterating(...)`` attribute into a method."""

    def on_package(self, *, pkg: griffe.Module, **kwargs: Any) -> None:  # noqa: ARG002
        """Replace the twins once the whole package, ``_client`` included, is loaded.

        Raises
        ------
        RuntimeError
            If ``water_ogcapi._sync`` holds no twin, so a renamed wrapper fails the
            strict docs build instead of emptying the API page.
        """
        if pkg.name != "water_ogcapi":
            return
        replaced = 0
        for cls in pkg["_sync"].classes.values():
            for name, member in list(cls.members.items()):
                value = getattr(member, "value", None)
                if not isinstance(value, griffe.ExprCall) or str(value.function) not in WRAPPERS:
                    continue
                target = pkg.modules_collection[value.arguments[0].canonical_path]
                cls.set_member(name, _twin(name, str(value.function), target))
                replaced += 1
        if not replaced:
            msg = "found no _blocking or _iterating twin in water_ogcapi._sync"
            raise RuntimeError(msg)
