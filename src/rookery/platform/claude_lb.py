"""Thin wrapper around the ``claude-lb`` CLI.

``claude-lb`` is a health-aware picker + probe + OAuth-refresh tool for
Claude Code Max profiles (the ``~/.claude-profiles/<name>/`` directories
that let one machine juggle multiple Anthropic accounts).
:class:`rookery.orchestrator.worker_backend.WorkerBackend` delegates
profile selection and OAuth refresh to it.

Delegating to ``claude-lb`` provides:

- Health-aware picking (ok / degraded / exhausted).
- Cached credentials-dir resolution via ``claude-lb show --json``.
- ``claude-lb refresh --expired`` that exchanges stored refresh tokens
  for fresh access tokens.

Every function here is best-effort: if ``claude-lb`` isn't on PATH, or
a subprocess fails, returns ``None`` / no-op and lets the caller fall
back to its previous logic. This keeps WorkerBackend runnable in
environments where claude-lb isn't installed (unit tests, fresh clones).

Tested against claude-lb 0.3.0.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

IS_WINDOWS = sys.platform == "win32"

_BIN_NAME = "claude-lb"
_PICK_TIMEOUT_S = 5.0
_SHOW_TIMEOUT_S = 5.0
_REFRESH_TIMEOUT_S = 10.0


#: Windows users-root scanned for ``<user>/.local/bin/claude-lb.exe`` when
#: the daemon's PATH doesn't already surface the binary. Overridable in tests.
_WINDOWS_USERS_ROOT = Path(r"C:\Users")


@lru_cache(maxsize=1)
def _discover_bin() -> str | None:
    """Locate the ``claude-lb`` executable.

    Resolution order (mirrors the claude.exe discovery in WorkerBackend):

    1. ``ROOKERY_LB_BIN`` env var — absolute path override.
    2. :func:`shutil.which` — hits when the daemon's own PATH already has it.
    3. Scan ``C:\\Users\\*\\.local\\bin\\claude-lb.exe`` — canonical user-scoped
       install, invisible to SYSTEM-run daemons by default.

    Returns the full executable path or ``None``. Cached — cheap to call
    repeatedly from the spawn hot path.
    """
    explicit = os.environ.get("ROOKERY_LB_BIN")
    if explicit and Path(explicit).is_file():
        return explicit

    via_path = shutil.which(_BIN_NAME)
    if via_path:
        return via_path

    if IS_WINDOWS and _WINDOWS_USERS_ROOT.is_dir():
        for user_home in _WINDOWS_USERS_ROOT.iterdir():
            candidate = user_home / ".local" / "bin" / f"{_BIN_NAME}.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def bin_dir() -> str | None:
    """Directory containing the ``claude-lb`` executable, or ``None``.

    Callers that need to prepend to PATH (e.g. WorkerBackend's Windows
    PATH augmentation for SYSTEM-run daemons) use this.
    """
    path = _discover_bin()
    return str(Path(path).parent) if path else None


def is_available() -> bool:
    """True if the ``claude-lb`` CLI is discoverable on this host."""
    return _discover_bin() is not None


def _run(args: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str] | None:
    """Run a claude-lb subcommand. Returns None on any failure path.

    Never raises — all exceptions become logged warnings. This makes the
    wrapper safe to call from hot paths without try/except at the call site.
    """
    bin_path = _discover_bin()
    if bin_path is None:
        return None
    try:
        return subprocess.run(  # noqa: S603 — argv is not user-shell-composed
            [bin_path, *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout_s,
        )
    except subprocess.CalledProcessError as exc:
        log.warning(
            "platform.claude_lb.subcommand_failed",
            args=args,
            rc=exc.returncode,
            stderr=(exc.stderr or "").strip()[:500],
        )
        return None
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning(
            "platform.claude_lb.subcommand_error",
            args=args,
            err=str(exc),
        )
        return None


def pick_profile(*, require_ok: bool = True) -> str | None:
    """Health-aware profile pick via ``claude-lb pick --json``.

    Parameters
    ----------
    require_ok:
        If True (default), pass ``--require-ok`` so only profiles with
        ``health=ok`` are considered. Callers that accept ``degraded``
        should pass False.

    Returns
    -------
    str | None
        Profile name (e.g. ``"roamhq"``), or ``None`` if claude-lb isn't
        installed / no healthy profile is available / the call failed.
        Caller is expected to fall back to its configured default when
        the result is None.
    """
    args = ["pick", "--json"]
    if require_ok:
        args.append("--require-ok")
    result = _run(args, timeout_s=_PICK_TIMEOUT_S)
    if result is None:
        return None
    try:
        name = json.loads(result.stdout)["data"]["name"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        log.warning(
            "platform.claude_lb.pick_parse_failed",
            stdout=result.stdout[:500],
            err=str(exc),
        )
        return None
    return str(name) if name else None


def resolve_config_dir(profile: str) -> str | None:
    """Return the ``CLAUDE_CONFIG_DIR`` for *profile* via ``claude-lb show --json``.

    ``claude-lb show`` returns the full ``.credentials.json`` path; the
    config dir is that file's parent (which is where claude CLI looks for
    both credentials and other per-profile state).

    Returns ``None`` on any failure; caller should fall back to its
    existing resolution logic (e.g. ``C:\\Users\\*`` scan).
    """
    result = _run(["show", profile, "--json"], timeout_s=_SHOW_TIMEOUT_S)
    if result is None:
        return None
    try:
        credentials_path = json.loads(result.stdout)["data"]["credentials_path"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        log.warning(
            "platform.claude_lb.show_parse_failed",
            profile=profile,
            stdout=result.stdout[:500],
            err=str(exc),
        )
        return None
    if not credentials_path:
        return None
    return str(Path(credentials_path).parent)


def refresh_expired() -> None:
    """Best-effort ``claude-lb refresh --expired``.

    Exchanges the stored refresh token for a fresh access token on any
    profile whose access token is past ``expiresAt``. Run this before
    spawning a worker to avoid silent 401s when the daemon has been idle
    long enough for a profile's 8-hour access token to lapse.

    Returns nothing; errors are logged but never raised. ~50 ms on a
    no-op call (all profiles fresh); seconds if refresh actually fires.
    """
    _run(["refresh", "--expired"], timeout_s=_REFRESH_TIMEOUT_S)


def get_access_token(profile: str) -> str | None:
    """Extract the raw OAuth access token from a profile's credentials file.

    ``claude-lb show`` deliberately doesn't emit the token on stdout (keeps
    it out of shell history / logs). For callers that need the actual
    bearer token — e.g. the Conductor's ``AsyncAnthropic(auth_token=...)``
    path — this helper:

    1. Asks claude-lb for the profile's ``credentials_path`` + ``token_source``
       (the JSON pointer, like ``claudeAiOauth.accessToken``).
    2. Reads and parses the credentials file.
    3. Navigates the JSON per ``token_source`` and returns the string.

    Returns ``None`` on any failure (claude-lb missing, profile unknown,
    file unreadable, token_source not present). Never raises — callers
    should fall back to ``CLAUDE_CODE_OAUTH_TOKEN`` env or API-key path.

    Warning: the returned string is a bearer credential. Don't log it,
    don't echo it to stdout, don't embed it in error messages.
    """
    result = _run(["show", profile, "--json"], timeout_s=_SHOW_TIMEOUT_S)
    if result is None:
        return None
    try:
        data = json.loads(result.stdout)["data"]
        credentials_path = data["credentials_path"]
        token_source = data.get("token_source", "claudeAiOauth.accessToken")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        log.warning(
            "platform.claude_lb.get_token_show_parse_failed",
            profile=profile,
            err=str(exc),
        )
        return None
    if not credentials_path:
        return None

    try:
        raw = Path(credentials_path).read_text(encoding="utf-8")
        creds = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "platform.claude_lb.get_token_read_failed",
            profile=profile,
            err=str(exc),
        )
        return None

    # Navigate token_source (e.g. "claudeAiOauth.accessToken") down the dict.
    node: object = creds
    for key in token_source.split("."):
        if not isinstance(node, dict) or key not in node:
            log.warning(
                "platform.claude_lb.get_token_source_missing",
                profile=profile,
                token_source=token_source,
                missing_key=key,
            )
            return None
        node = node[key]
    if not isinstance(node, str) or not node:
        return None
    return node


def role_session_env() -> dict[str, str] | None:
    """Build a per-role env override for a fresh ``claude_agent_sdk`` session.

    Composes the full rotation pipeline for a single agent call:

    1. ``refresh_expired()`` — rotate any profile past ``expiresAt``.
    2. ``pick_profile(require_ok=True)`` — health-aware pick. Sticky by
       default: parallel callers in the same 300s window tend to reuse
       one profile, improving Anthropic's cache affinity.
    3. ``get_access_token(profile)`` — extract the bearer token.

    Returns a dict suitable to pass as ``ClaudeAgentOptions.env``, which
    the SDK merges on top of the inherited process env. Currently this
    is a single key::

        {"CLAUDE_CODE_OAUTH_TOKEN": "<access-token>"}

    Returns ``None`` when claude-lb is unavailable, no healthy profile
    exists, or token extraction fails. Caller should fall back to the
    inherited env (which typically has ``CLAUDE_CODE_OAUTH_TOKEN`` set by
    the operator's shell).
    """
    refresh_expired()
    profile = pick_profile(require_ok=True)
    if profile is None:
        return None
    token = get_access_token(profile)
    if not token:
        return None
    return {"CLAUDE_CODE_OAUTH_TOKEN": token}


__all__ = [
    "bin_dir",
    "get_access_token",
    "is_available",
    "pick_profile",
    "refresh_expired",
    "resolve_config_dir",
    "role_session_env",
]
