"""Verdict adapter registry (G4).

``VERDICT_ADAPTERS`` is the dict of built-in adapters.  :func:`get_verdict_adapter`
is the single lookup function used throughout the daemon.

Plugin discovery
----------------
Third-party packages can register custom adapters via the
``claude_fleet.verdict_adapters`` entry-points group::

    # pyproject.toml
    [project.entry-points."claude_fleet.verdict_adapters"]
    my-adapter = "mypackage.adapters:MyAdapter"

The adapter class must be a zero-argument-constructible :class:`VerdictAdapter`
subclass (or accept only keyword arguments with defaults).

If the entry-points group does not exist the discovery step is skipped silently —
no error is raised on hosts without any plugins installed.
"""

from __future__ import annotations

from claude_fleet.adapters.base import VerdictAdapter
from claude_fleet.adapters.exit_code import ExitCodeAdapter
from claude_fleet.adapters.json_result import JsonResultAdapter
from claude_fleet.adapters.marker_file import MarkerFileAdapter


class UnknownVerdictAdapter(Exception):
    """Raised by :func:`get_verdict_adapter` for an unregistered name."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"unknown verdict adapter {name!r}. "
            f"Registered built-ins: marker-file, exit-code, json-result. "
            f"Install a plugin or check your 'verdict_adapter' config key."
        )
        self.name = name


# Built-in registry.  Keys are the kebab-case names used in config / frontmatter.
# Values are *classes* (not instances) — get_verdict_adapter() instantiates them.
VERDICT_ADAPTERS: dict[str, type[VerdictAdapter]] = {
    "marker-file": MarkerFileAdapter,
    "exit-code": ExitCodeAdapter,
    "json-result": JsonResultAdapter,
}


def get_verdict_adapter(name: str) -> VerdictAdapter:
    """Return a fresh :class:`VerdictAdapter` instance for *name*.

    Lookup order:

    1. ``VERDICT_ADAPTERS`` built-in dict.
    2. ``importlib.metadata`` entry-points under
       ``claude_fleet.verdict_adapters``.

    Raises:
        UnknownVerdictAdapter: if no adapter is registered for *name*.
        TypeError: if a discovered plugin class requires constructor arguments
            that cannot be satisfied.

    Note:
        ``ExitCodeAdapter`` is registered but requires a constructor argument
        (``exit_code_fn``).  Instantiating it via this function will raise
        ``TypeError``.  Callers that need ``ExitCodeAdapter`` should
        instantiate it directly with the appropriate callable.
    """
    if name in VERDICT_ADAPTERS:
        return VERDICT_ADAPTERS[name]()  # type: ignore[call-arg]

    # Plugin discovery via entry-points.
    try:
        from importlib.metadata import entry_points  # noqa: PLC0415

        eps = entry_points(group="claude_fleet.verdict_adapters")
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
