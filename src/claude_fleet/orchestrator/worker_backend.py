"""WorkerBackend: spawn parcel workers via the :mod:`claude_fleet.platform` API.

Each worker runs in an isolated git worktree under
``<worktrees_root>/<job.id>/``. Completion is signalled by the worker
writing ``PARCEL_DONE-<job_id>.md`` at the worktree root (per-parcel
unique naming). Legacy plain ``PARCEL_DONE.md`` is still accepted for
backward compatibility with worktrees predating the naming-convention
change.

Pipeline per spawn::

    build env (profile rotation, CLAUDE_CONFIG_DIR, PATH augment, git safe.directory)
        └─► ensure_worktree     (git worktree add, uses env)
            └─► find_parcel_prompt  (parcels/**/<id>.md, shallowest)
                └─► spawn_headless_claude  (detached claude -p, writes .pid/.winpid)

Safety rails:

- If ``ANTHROPIC_API_KEY`` is set in the host env, the spawn is refused
  (OAuth-only policy — parcel sessions must authenticate via Claude Max profiles,
  not API keys, to ensure they use the correct plan limits and auth context).
- Profile-based auth wins: ``CLAUDE_CODE_OAUTH_TOKEN`` stripped from the
  per-spawn env so the claude CLI reads ``<profile>/.credentials.json``.

Paired with :class:`claude_fleet.orchestrator.land_backend.LandBackend` —
worker phase runs; land phase merges to main.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import uuid
from pathlib import Path

import structlog

from claude_fleet.orchestrator.backend import Job, OrchestratorBackend, WorkerHandle
from claude_fleet.platform import claude_lb
from claude_fleet.platform.headless_spawn import spawn_headless_claude
from claude_fleet.platform.parcel_prompt import find_parcel_prompt
from claude_fleet.platform.worktree_dir import ensure_worktree
from claude_fleet.worktree import GitWorktreeLifecycle, WorktreeLifecycle

log = structlog.get_logger(__name__)

IS_WINDOWS = sys.platform == "win32"


def _inject_git_safe_directory(env: dict[str, str]) -> None:
    """Append ``safe.directory=*`` to ``env`` via ``GIT_CONFIG_COUNT``.

    Cross-user git operations (e.g. pm2 daemon running as SYSTEM, repo owned
    by an interactive user) are rejected with "fatal: detected dubious
    ownership" unless the repo is allow-listed via ``safe.directory``. The
    per-invocation env protocol (``GIT_CONFIG_COUNT`` + ``GIT_CONFIG_KEY_N`` +
    ``GIT_CONFIG_VALUE_N``) injects config entries without touching the
    global file, so this doesn't bleed into unrelated git usage.

    Wildcard ``*`` is appropriate here because the daemon's spawn path is
    scoped to a trusted set of parcel worktrees it created itself; the
    ownership check adds no meaningful protection in this context.
    """
    try:
        existing_count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        existing_count = 0
    idx = existing_count
    env["GIT_CONFIG_COUNT"] = str(existing_count + 1)
    env[f"GIT_CONFIG_KEY_{idx}"] = "safe.directory"
    env[f"GIT_CONFIG_VALUE_{idx}"] = "*"


def _resolve_claude_config_dir(profile: str | None) -> str | None:
    """Resolve the ``CLAUDE_CONFIG_DIR`` the spawned child claude should use.

    Claude CLI stores OAuth credentials in ``<config-dir>/.credentials.json``.
    Per-user profiles live at ``~/.claude-profiles/<name>/`` and let one
    machine juggle multiple Anthropic accounts without re-auth.

    Resolution (first hit wins):

    1. ``CLAUDE_FLEET_CONFIG_DIR`` env var — absolute path override. Takes
       precedence over everything so operators can point the daemon at a
       purpose-built credentials dir without editing config.
    2. ``CLAUDE_FLEET_PROFILE`` env var — profile name override.
    3. ``claude-lb show <profile> --json`` when the CLI is installed. This
       is the canonical resolver: health-cached, single source of truth
       for profile → credentials-path mapping.
    4. ``profile`` argument scanned under ``C:\\Users\\*\\.claude-profiles\\
       <name>``. Retained as a fallback for hosts without claude-lb.
    5. If profile is None or not found: fall back to the user's default
       ``~/.claude/`` dir.
    """
    explicit = os.environ.get("CLAUDE_FLEET_CONFIG_DIR")
    if explicit and Path(explicit).is_dir():
        return explicit

    env_profile = os.environ.get("CLAUDE_FLEET_PROFILE")
    effective_profile = env_profile or profile

    # Preferred: delegate to claude-lb (cached, health-aware).
    if effective_profile:
        via_lb = claude_lb.resolve_config_dir(effective_profile)
        if via_lb is not None:
            return via_lb

    users_root = Path(r"C:\Users")
    if not users_root.is_dir():
        return None

    if effective_profile:
        for user_home in users_root.iterdir():
            candidate = user_home / ".claude-profiles" / effective_profile
            if (candidate / ".credentials.json").is_file():
                return str(candidate)

    # Fallback: default profile under ~/.claude
    for user_home in users_root.iterdir():
        candidate = user_home / ".claude"
        if (candidate / ".credentials.json").is_file():
            return str(candidate)
    return None


def _discover_claude_bin_dir() -> str | None:
    """Locate the directory containing the ``claude`` CLI on Windows.

    Resolution order:

    1. ``CLAUDE_FLEET_BIN_DIR`` env var (operator override, e.g. set in
       pm2 ecosystem.config.js when auto-detect doesn't fit)
    2. ``shutil.which("claude")`` — hits when the daemon's own PATH already
       includes claude (interactive dev runs)
    3. Scan ``C:\\Users\\*\\.local\\bin\\claude.exe`` — the canonical
       user-scoped install location. Essential for SYSTEM-run daemons
       (pm2 default) which don't see any user's .local/bin by default.

    Returns the directory containing ``claude.exe``, or ``None`` if not
    found. Caller is responsible for prepending to PATH.
    """
    explicit = os.environ.get("CLAUDE_FLEET_BIN_DIR")
    if explicit and Path(explicit).is_dir():
        return explicit

    via_path = shutil.which("claude")
    if via_path:
        return str(Path(via_path).parent)

    users_root = Path(r"C:\Users")
    if users_root.is_dir():
        for user_home in users_root.iterdir():
            candidate = user_home / ".local" / "bin" / "claude.exe"
            if candidate.is_file():
                return str(candidate.parent)
    return None


def _augment_windows_path(current_path: str) -> str:
    """Prepend Git Bash POSIX utilities + claude/claude-lb bin dirs to a Windows PATH.

    The daemon inherits whatever PATH pm2 / Task Scheduler started it with,
    which on SYSTEM or a fresh login shell may omit:

    - ``claude.exe`` — typically user-scoped (``~/.local/bin``), so SYSTEM
      never sees it; ``Popen(["claude", ...])`` fails with "No such file
      or directory".
    - ``claude-lb.exe`` — same user-scoped install story; WorkerBackend
      shells out to it for profile picking + OAuth refresh.
    - Git Bash utilities — still nice to have for any sibling tools the
      worker shells out to, even though the worker itself no longer runs
      under bash.

    Prepending rather than replacing means host-specific overrides still win.
    """
    dirs_to_prepend: list[str] = []
    for root in (r"C:\Program Files\Git", r"C:\Program Files (x86)\Git"):
        usr_bin = Path(root) / "usr" / "bin"
        if usr_bin.is_dir():
            dirs_to_prepend.append(str(usr_bin))
            mingw_dir = Path(root) / "mingw64" / "bin"
            if mingw_dir.is_dir():
                dirs_to_prepend.append(str(mingw_dir))
            break

    claude_dir = _discover_claude_bin_dir()
    if claude_dir is not None:
        dirs_to_prepend.append(claude_dir)

    claude_lb_dir = claude_lb.bin_dir()
    if claude_lb_dir is not None:
        dirs_to_prepend.append(claude_lb_dir)

    # Deduplicate against the existing PATH (case-insensitive on Windows).
    existing = [p for p in current_path.split(os.pathsep) if p]
    existing_lower = {os.path.normcase(p) for p in existing}
    new_entries = [
        d for d in dirs_to_prepend if os.path.normcase(d) not in existing_lower
    ]
    if not new_entries:
        return current_path
    return os.pathsep.join(new_entries + existing)


class WorkerBackend(OrchestratorBackend):
    """Spawn parcel workers as detached ``claude -p`` subprocesses.

    Parameters
    ----------
    repo_root:
        Path to the project repo.
    worktrees_root:
        Directory that holds sibling worktrees. Defaults to
        ``<repo_root>/../claude-fleet-worktrees``.
    env_overrides:
        Extra environment variables layered onto :data:`os.environ` before
        spawning. Tests use this to skip the OAuth check. Empty-string
        values mean *unset* (key removed from the spawn env).
    shutdown_grace_s:
        Seconds to wait on terminate() before escalating to kill.
    claude_profile:
        Default claude CLI profile to use for spawned parcel sessions
        (resolves to ``C:\\Users\\<user>\\.claude-profiles\\<name>``).
        ``None`` means "use the claude default (``~/.claude``)".
        Env vars ``CLAUDE_FLEET_CONFIG_DIR`` (absolute path) and
        ``CLAUDE_FLEET_PROFILE`` (profile name) override this per
        daemon instance. Env var ``CLAUDE_FLEET_PROFILES`` (comma-list)
        cycles profiles round-robin per spawn.
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        worktrees_root: Path | None = None,
        env_overrides: dict[str, str] | None = None,
        shutdown_grace_s: int = 30,
        claude_profile: str | None = "mknv74",
        worktree_lifecycle: WorktreeLifecycle | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.worktrees_root = Path(
            worktrees_root or self.repo_root.parent / "claude-fleet-worktrees"
        )
        # G1: If a WorktreeLifecycle is injected, it takes responsibility for
        # creating/querying worktrees.  Otherwise the legacy ensure_worktree
        # helper is used so existing deployments are unaffected.
        self._worktree_lifecycle: WorktreeLifecycle | None = worktree_lifecycle
        self.env_overrides = dict(env_overrides or {})
        self.shutdown_grace_s = shutdown_grace_s
        self.claude_profile = claude_profile
        # Profile round-robin: env CLAUDE_FLEET_PROFILES="a,b,c" cycles
        # through the named profiles on each spawn. When unset, falls back
        # to the single `claude_profile`. Useful for spreading parcel
        # dispatch across multiple Max accounts to avoid per-account rate limits.
        profiles_env = os.environ.get("CLAUDE_FLEET_PROFILES", "").strip()
        self._profile_ring: list[str] = (
            [p.strip() for p in profiles_env.split(",") if p.strip()]
            if profiles_env
            else []
        )
        self._profile_idx = 0
        self._pids: dict[str, int] = {}

    # -- helpers -----------------------------------------------------------

    def _worktree(self, job_id: str) -> Path:
        return self.worktrees_root / job_id

    def _log_path(self, job_id: str) -> Path:
        return self.worktrees_root / "logs" / f"{job_id}.log"

    def _next_profile(self) -> str | None:
        """Pick the profile to use for the *next* spawn.

        Preference order:

        1. ``claude-lb pick --require-ok`` when the CLI is installed.
           Health-aware (skips degraded / exhausted profiles) and sticky
           by default (better cache affinity on Anthropic's side than
           naive round-robin).
        2. Round-robin across ``CLAUDE_FLEET_PROFILES`` when set — fallback
           for hosts without claude-lb.
        3. The configured default (``self.claude_profile``).
        """
        picked = claude_lb.pick_profile(require_ok=True)
        if picked is not None:
            return picked
        if self._profile_ring:
            profile = self._profile_ring[self._profile_idx % len(self._profile_ring)]
            self._profile_idx += 1
            return profile
        return self.claude_profile

    def _build_env(self, profile_override: str | None = None) -> dict[str, str]:
        """Merge os.environ with overrides. Empty-string override means *unset*.

        On Windows, also prepends claude bin dir + Git POSIX utility dirs to
        ``PATH``. Daemons started from pm2 / Task Scheduler inherit a bare
        Windows ``PATH`` that doesn't include ``~/.local/bin`` (where
        ``claude.exe`` lives) — without this, ``Popen(["claude", ...])``
        would fail with "No such file or directory". Idempotent: re-running
        is a no-op if the dirs are already on PATH.

        Also:

        - Points ``CLAUDE_CONFIG_DIR`` at the resolved profile dir so the
          child claude picks up the right ``.credentials.json``.
        - Strips any ``CLAUDE_CODE_OAUTH_TOKEN`` inherited from the daemon's
          own env; otherwise the env-var would override profile-based auth
          inside claude CLI. (See ``CLAUDE_FLEET_PROFILE`` / ``CLAUDE_FLEET_CONFIG_DIR``
          for per-instance overrides.)
        - Injects ``GIT_CONFIG_COUNT + safe.directory=*`` so the in-process
          ``git worktree add`` inside :func:`ensure_worktree` works when
          the daemon runs as SYSTEM against a repo owned by an interactive
          user.
        """
        env = dict(os.environ)
        for key, value in self.env_overrides.items():
            if value == "":
                env.pop(key, None)
            else:
                env[key] = value
        if IS_WINDOWS:
            env["PATH"] = _augment_windows_path(env.get("PATH", ""))
            effective_profile = profile_override or self.claude_profile
            claude_cfg = _resolve_claude_config_dir(effective_profile)
            if claude_cfg is not None and "CLAUDE_CONFIG_DIR" not in env:
                env["CLAUDE_CONFIG_DIR"] = claude_cfg
            # Stale OAuth token in the daemon's env (e.g. pm2 inherited a
            # rotated token from SYSTEM env) will override profile-based
            # auth because claude CLI prefers the env var. Strip it so
            # profile .credentials.json wins.
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        _inject_git_safe_directory(env)
        # Hard safety rail: parcels run on OAuth only (Claude Max plan). Using
        # an API key instead would route sessions through a different billing
        # context, bypass rate-limit pooling, and may violate per-account policy.
        if env.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is set; OAuth-only policy forbids this. "
                "Unset ANTHROPIC_API_KEY before starting the daemon — parcel "
                "sessions authenticate via ~/.claude-profiles/<name>/.credentials.json."
            )
        return env

    # -- OrchestratorBackend ABC ------------------------------------------

    async def spawn(self, job: Job) -> WorkerHandle:
        worker_id = f"local-{uuid.uuid4().hex[:8]}"
        log_path = self._log_path(job.id)

        # Rotate any stale access tokens before picking a profile.
        # No-op and fast (~50ms) when nothing needs refresh.
        await asyncio.to_thread(claude_lb.refresh_expired)

        profile = self._next_profile()
        env = self._build_env(profile_override=profile)

        # Resolve prompt before we touch git — fail fast on a typo'd job id.
        try:
            prompt_path = find_parcel_prompt(self.repo_root / "parcels", job.id)
        except FileNotFoundError as exc:
            raise RuntimeError(f"spawn failed for {job.id}: {exc}") from exc

        # Create / reuse the parcel worktree.
        # When a WorktreeLifecycle is injected (G1), delegate to it so that
        # the lifecycle ABC owns worktree creation end-to-end.  Otherwise fall
        # back to the platform-level ensure_worktree helper, passing env so
        # SYSTEM-run git sees the safe.directory injection.
        try:
            if self._worktree_lifecycle is not None:
                worktree = await self._worktree_lifecycle.create(job)
            else:
                worktree = await asyncio.to_thread(
                    ensure_worktree, job.id, self.repo_root, env=env
                )
        except (RuntimeError, Exception) as exc:
            raise RuntimeError(f"spawn failed for {job.id}: {exc}") from exc

        log.info(
            "orchestrator.worker_backend.spawn",
            job_id=job.id,
            worker_id=worker_id,
            worktree=str(worktree),
            log_path=str(log_path),
            profile=profile,
        )

        try:
            result = await asyncio.to_thread(
                spawn_headless_claude,
                worktree=worktree,
                prompt_path=prompt_path,
                log_path=log_path,
                env=env,
            )
        except (OSError, FileNotFoundError) as exc:
            raise RuntimeError(f"spawn failed for {job.id}: {exc}") from exc

        self._pids[worker_id] = result.pid

        return WorkerHandle(
            job_id=job.id,
            worker_id=worker_id,
            pid=result.pid,
            worktree=worktree,
            log_path=log_path,
        )

    async def is_alive(self, handle: WorkerHandle) -> bool:
        pid = handle.pid
        if pid is None or pid < 0:
            return False
        return await asyncio.to_thread(_pid_alive, pid)

    async def harvest(self, handle: WorkerHandle) -> dict[str, object] | None:
        # Preferred convention: parcels/done/<job_id>.md (committed-friendly,
        # not affected by root .gitignore). Also accepted: the MODULE-NN short
        # form (e.g. parcels/done/BENCH-04.md for job BENCH-04-batch-scheduler)
        # because parcel prompts in MODULE-NN-slug naming don't always specify
        # which form to use, and historical worktrees ship a mix of both.
        # Legacy fallbacks: PARCEL_DONE-<id>.md at worktree root (per-parcel
        # unique), then plain PARCEL_DONE.md.
        short_id = "-".join(handle.job_id.split("-")[:2])
        candidates = [
            handle.worktree / "parcels" / "done" / f"{handle.job_id}.md",
            handle.worktree / "parcels" / "done" / f"{short_id}.md",
            handle.worktree / f"PARCEL_DONE-{handle.job_id}.md",
            handle.worktree / f"PARCEL_DONE-{short_id}.md",
            handle.worktree / "PARCEL_DONE.md",
        ]
        done_file = next((p for p in candidates if p.exists()), None)
        if done_file is None:
            return None
        try:
            body = done_file.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning(
                "orchestrator.worker_backend.harvest_read_failed",
                job_id=handle.job_id,
                done_file=str(done_file),
                err=str(exc),
            )
            return None
        tail = _read_tail(handle.log_path, n=200) if handle.log_path.exists() else ""
        return {
            "status": "done",
            "parcel_done_md": body,
            "stdout_tail": tail,
        }

    async def terminate(self, handle: WorkerHandle) -> None:
        pid = handle.pid
        if pid is None or pid < 0:
            return
        try:
            await asyncio.to_thread(_terminate_pid, pid, self.shutdown_grace_s)
        finally:
            self._pids.pop(handle.worker_id, None)


def _pid_alive(pid: int) -> bool:
    """Cross-platform liveness check for a Windows or POSIX PID.

    On Windows ``os.kill(pid, 0)`` is broken — signal 0 isn't a valid
    Windows signal so it raises ``OSError(WinError 87)`` even for live
    processes. We use ``OpenProcess(QUERY_LIMITED_INFORMATION)`` plus
    ``GetExitCodeProcess`` instead: STILL_ACTIVE (259) means alive.

    On POSIX, signal 0 is the documented liveness probe.
    """

    if IS_WINDOWS:
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_pid_alive(pid: int) -> bool:
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # noqa: N806 — Win32 constant
    STILL_ACTIVE = 259  # noqa: N806 — Win32 constant

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid)
    )
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        if not ok:
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _terminate_pid(pid: int, grace_s: int) -> None:
    try:
        if IS_WINDOWS:
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return

    for _ in range(max(1, grace_s) * 10):
        if not _pid_alive(pid):
            return
        import time  # noqa: PLC0415
        time.sleep(0.1)

    try:
        if IS_WINDOWS:
            os.kill(pid, signal.SIGTERM)  # Windows maps to TerminateProcess
        else:
            sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            os.kill(pid, sigkill)
    except (ProcessLookupError, OSError):
        pass


def _read_tail(path: Path, *, n: int) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            chunk = min(size, 16_384)
            fh.seek(size - chunk, 0)
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = data.splitlines()[-n:]
    return "\n".join(lines)


__all__ = ["WorkerBackend"]
