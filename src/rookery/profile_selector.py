"""Profile selector ABC + built-in implementations for G5 (claude-lb integration).

Provides a clean abstraction over "which Claude OAuth profile should this
worker spawn use?". Two implementations ship out of the box:

- :class:`EnvVarSelector` — round-robins over a static list, defaulting to
  ``ROOKERY_PROFILES`` env var. No external binary required.
- :class:`ClaudeLbSelector` — delegates to the ``claude-lb`` binary for
  health-aware picking and OAuth refresh. Requires ``claude-lb`` on PATH
  (or a configured binary path). See ``[lb]`` optional-dependency.

Callers that need only a profile name and don't care about selector mechanics
should depend on :class:`ProfileSelector` and accept either implementation.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from abc import ABC, abstractmethod

import structlog
from pydantic import BaseModel

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class ProfileInfo(BaseModel):
    """Describes a single Claude OAuth profile to use for a worker spawn.

    Attributes:
        name: Human-readable profile name (e.g. ``"roamhq"``).
        env:  Environment variables to inject into the worker subprocess.
              Typically ``{"CLAUDE_CONFIG_DIR": "/path/to/profile"}`` or
              ``{"CLAUDE_CODE_OAUTH_TOKEN": "<bearer-token>"}``. Empty dict
              means "no extra env" — inherit from the daemon's own env.
    """

    name: str
    env: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ClaudeLbBinaryMissing(RuntimeError):
    """Raised when ``ClaudeLbSelector`` cannot find the ``claude-lb`` binary."""


class ProfileListEmpty(ValueError):
    """Raised when ``EnvVarSelector`` has an empty profile list on ``pick()``."""


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class ProfileSelector(ABC):
    """Abstract base class for profile selection strategies.

    Implementations must be safe to call concurrently from an async event
    loop — i.e. no blocking I/O in ``pick()`` without wrapping in
    ``asyncio.to_thread``.
    """

    @abstractmethod
    async def pick(self) -> ProfileInfo:
        """Return the next profile to use for a worker spawn.

        Raises:
            ProfileListEmpty: if no profiles are configured (EnvVarSelector).
            ClaudeLbBinaryMissing: if the claude-lb binary is not on PATH.
            RuntimeError: if the subprocess fails or returns unexpected output.
        """


# ---------------------------------------------------------------------------
# EnvVarSelector
# ---------------------------------------------------------------------------


class EnvVarSelector(ProfileSelector):
    """Round-robin selector backed by a static list of profile names.

    If *profiles* is not provided (or is empty), the constructor reads
    ``ROOKERY_PROFILES`` from the environment at instantiation time.

    Parameters
    ----------
    profiles:
        Explicit list of profile names. If ``None`` or empty, falls back to
        ``os.environ["ROOKERY_PROFILES"].split(",")`` at construction.

    Example
    -------
    >>> sel = EnvVarSelector(profiles=["alpha", "beta"])
    >>> # successive picks: alpha, beta, alpha, beta, ...
    """

    def __init__(self, profiles: list[str] | None = None) -> None:
        if not profiles:
            raw = os.environ.get("ROOKERY_PROFILES", "").strip()
            profiles = [p.strip() for p in raw.split(",") if p.strip()] if raw else []
        self._profiles: list[str] = profiles
        self._index: int = 0

    async def pick(self) -> ProfileInfo:
        """Return the next profile from the ring buffer.

        Raises:
            ProfileListEmpty: if the profile list is empty.
        """
        if not self._profiles:
            raise ProfileListEmpty(
                "EnvVarSelector has no profiles configured. "
                "Set ROOKERY_PROFILES=<comma-list> or pass profiles= explicitly."
            )
        name = self._profiles[self._index % len(self._profiles)]
        self._index += 1
        log.debug("profile_selector.env_var.picked", profile=name, index=self._index - 1)
        return ProfileInfo(name=name, env={})


# ---------------------------------------------------------------------------
# ClaudeLbSelector
# ---------------------------------------------------------------------------

_DEFAULT_BINARY = "claude-lb"
_SUBPROCESS_TIMEOUT_S = 10.0


class ClaudeLbSelector(ProfileSelector):
    """Profile selector that delegates to the ``claude-lb`` binary.

    Uses ``claude-lb pick --auto-refresh --json`` (plus any extra *pick_args*)
    to obtain a health-aware, freshly-refreshed profile. The binary is
    invoked as an async subprocess so it does not block the event loop.

    Parameters
    ----------
    binary:
        Path or name of the ``claude-lb`` executable. Defaults to
        ``"claude-lb"`` (resolved via PATH). Override with an absolute path
        if the binary lives outside PATH (e.g. ``/home/user/.local/bin/claude-lb``).
    pick_args:
        Extra CLI arguments forwarded after ``pick``. Defaults to
        ``["--auto-refresh"]``. Pass ``[]`` to call ``claude-lb pick --json``
        with no extras.

    Notes
    -----
    The integration assumes ``claude-lb`` is on PATH and supports the
    ``pick --json`` subcommand (tested against 0.3.x). The ``[lb]``
    optional-dependency extra in ``pyproject.toml`` is intentionally empty
    (no package on PyPI); users install the binary independently.
    """

    def __init__(
        self,
        binary: str = _DEFAULT_BINARY,
        pick_args: list[str] | None = None,
    ) -> None:
        self.binary = binary
        self.pick_args: list[str] = pick_args if pick_args is not None else ["--auto-refresh"]

    def _resolve_binary(self) -> str:
        """Return the resolved binary path, or raise :class:`ClaudeLbBinaryMissing`."""
        # If the caller supplied an absolute/relative path, trust it directly.
        if self.binary != _DEFAULT_BINARY:
            return self.binary
        found = shutil.which(self.binary)
        if found is None:
            raise ClaudeLbBinaryMissing(
                "claude-lb not on PATH; install via 'pip install claude-lb' or "
                "set the binary path explicitly in ClaudeLbConfig.binary."
            )
        return found

    async def pick(self) -> ProfileInfo:
        """Invoke ``claude-lb pick --json`` and parse the result.

        Returns:
            :class:`ProfileInfo` with ``name`` from the JSON response and
            ``env`` left empty (the binary returns profile metadata but not
            credentials inline).

        Raises:
            ClaudeLbBinaryMissing: binary not on PATH.
            RuntimeError: subprocess returned non-zero or stdout was not valid JSON.
        """
        try:
            bin_path = self._resolve_binary()
        except ClaudeLbBinaryMissing:
            raise

        cmd = [bin_path, "pick", *self.pick_args, "--json"]
        log.debug("profile_selector.claude_lb.invoking", cmd=cmd)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=_SUBPROCESS_TIMEOUT_S
            )
        except FileNotFoundError as exc:
            raise ClaudeLbBinaryMissing(
                f"claude-lb binary not found at {bin_path!r}: {exc}. "
                "Install via 'pip install claude-lb' or set the binary path explicitly."
            ) from exc
        except TimeoutError as exc:
            raise RuntimeError(
                f"claude-lb pick timed out after {_SUBPROCESS_TIMEOUT_S}s"
            ) from exc

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            raise RuntimeError(
                f"claude-lb pick exited with code {proc.returncode}. "
                f"stderr: {stderr.strip()[:500]}"
            )

        try:
            payload = json.loads(stdout)
            name = payload["data"]["name"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"claude-lb pick returned unexpected JSON: {exc}. "
                f"stdout: {stdout.strip()[:500]}"
            ) from exc

        if not name:
            raise RuntimeError(
                f"claude-lb pick returned an empty profile name. stdout: {stdout.strip()[:500]}"
            )

        log.debug("profile_selector.claude_lb.picked", profile=name)
        return ProfileInfo(name=str(name), env={})


__all__ = [
    "ClaudeLbBinaryMissing",
    "ClaudeLbSelector",
    "EnvVarSelector",
    "ProfileInfo",
    "ProfileListEmpty",
    "ProfileSelector",
]
