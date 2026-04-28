"""Verdict adapter registry (G4 + v0.3 chain/db additions).

``VERDICT_ADAPTERS`` is the dict of built-in adapters.  :func:`get_verdict_adapter`
is the single lookup function used throughout the daemon.

Some adapters need runtime context (e.g. :class:`DbResultAdapter` needs
the queue's DB path).  Pass it via the ``db_path`` keyword argument; the
registry threads it through to the adapters that ask for it.  Adapters
that don't need the context ignore the kwarg.

Plugin discovery
----------------
Third-party packages can register custom adapters via the
``rookery.verdict_adapters`` entry-points group::

    # pyproject.toml
    [project.entry-points."rookery.verdict_adapters"]
    my-adapter = "mypackage.adapters:MyAdapter"

The adapter class must be either zero-argument-constructible OR accept a
``db_path: Path`` keyword argument.

If the entry-points group does not exist the discovery step is skipped silently —
no error is raised on hosts without any plugins installed.
"""

from __future__ import annotations

from pathlib import Path

from rookery.adapters.base import VerdictAdapter
from rookery.adapters.chain import ChainedAdapter
from rookery.adapters.db import DbResultAdapter
from rookery.adapters.exit_code import ExitCodeAdapter
from rookery.adapters.json_result import JsonResultAdapter
from rookery.adapters.marker_file import MarkerFileAdapter


class UnknownVerdictAdapter(Exception):
    """Raised by :func:`get_verdict_adapter` for an unregistered name."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"unknown verdict adapter {name!r}. "
            f"Registered built-ins: chain, db, marker-file, exit-code, json-result. "
            f"Install a plugin or check your 'verdict_adapter' config key."
        )
        self.name = name


# Built-in registry.  Keys are the kebab-case names used in config / frontmatter.
# Values are *classes* (not instances) — get_verdict_adapter() instantiates them.
#
# v0.3 additions:
#   ``chain``  — DbResultAdapter → MarkerFileAdapter (default)
#   ``db``     — direct query of parcel_results table
VERDICT_ADAPTERS: dict[str, type[VerdictAdapter]] = {
    "marker-file": MarkerFileAdapter,
    "exit-code": ExitCodeAdapter,
    "json-result": JsonResultAdapter,
    "db": DbResultAdapter,
    "chain": ChainedAdapter,
}


def get_verdict_adapter(
    name: str,
    *,
    db_path: Path | None = None,
) -> VerdictAdapter:
    """Return a fresh :class:`VerdictAdapter` instance for *name*.

    Args:
        name: Adapter key (one of the built-in names or an entry-points plugin).
        db_path: Path to the queue's SQLite db.  Required when *name* is
            ``db`` or ``chain``; ignored for other adapters.

    Lookup order:

    1. Built-in adapters (``VERDICT_ADAPTERS``) handled directly so we can
       construct context-aware ones with the right kwargs.
    2. ``importlib.metadata`` entry-points under ``rookery.verdict_adapters``.

    Raises:
        UnknownVerdictAdapter: if no adapter is registered for *name*.
        ValueError: if *db_path* is required but not supplied.
        TypeError: if a discovered plugin class can't be instantiated.

    Note:
        ``ExitCodeAdapter`` requires a runtime callable (``exit_code_fn``)
        that this registry can't supply.  Callers that need it must
        instantiate it directly.
    """
    # v0.3 context-aware built-ins.
    if name == "db":
        if db_path is None:
            raise ValueError("verdict_adapter 'db' requires db_path")
        return DbResultAdapter(db_path)
    if name == "chain":
        if db_path is None:
            raise ValueError("verdict_adapter 'chain' requires db_path")
        # Default chain: prefer DB-direct (v0.3 helper), fall back to
        # marker file (legacy parcels) so the daemon harvests both formats
        # without operator action.
        return ChainedAdapter([DbResultAdapter(db_path), MarkerFileAdapter()])

    if name in VERDICT_ADAPTERS:
        return VERDICT_ADAPTERS[name]()  # type: ignore[call-arg]

    # Plugin discovery via entry-points.
    try:
        from importlib.metadata import entry_points  # noqa: PLC0415

        eps = entry_points(group="rookery.verdict_adapters")
        for ep in eps:
            if ep.name == name:
                adapter_cls: type[VerdictAdapter] = ep.load()
                return adapter_cls()
    except Exception:  # noqa: BLE001
        # Never fail hard on a broken plugin — propagate UnknownVerdictAdapter
        # below so the caller gets a clean error message.
        pass

    raise UnknownVerdictAdapter(name)


__all__ = [
    "VERDICT_ADAPTERS",
    "UnknownVerdictAdapter",
    "get_verdict_adapter",
]
