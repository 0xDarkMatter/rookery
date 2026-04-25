"""Orchestrator configuration model.

Lives in its own module to keep the queue self-contained — the orchestrator
runs host-side and may be used in contexts where no external config file is
present (headless pm2 services, tests, ad-hoc CLI invocation).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ClaudeLbConfig(BaseModel):
    """Configuration for the optional ``claude-lb`` integration (G5).

    When ``enabled=True``, the orchestrator constructs a
    :class:`~claude_fleet.profile_selector.ClaudeLbSelector` from *binary*
    and *pick_args* instead of the default
    :class:`~claude_fleet.profile_selector.EnvVarSelector`.

    Attributes:
        enabled:   Whether to use ``claude-lb`` for profile selection.
        binary:    Path or name of the ``claude-lb`` executable. Defaults
                   to ``"claude-lb"`` (resolved via PATH).
        pick_args: Extra CLI arguments forwarded to ``claude-lb pick``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    binary: str = "claude-lb"
    pick_args: list[str] = ["--auto-refresh"]


class AuditLoopConfig(BaseModel):
    """Tuning knobs for the audit-and-fix iteration cycle.

    Attributes:
        max_iter: Cap on total audit iterations (including the first).
            Reaching this cap with a still-failing audit fires
            :meth:`Notifier.fix_exhausted`.
        audit_backoff_s: Seconds to wait between polling ticks for
            ``PARCEL_DONE`` / audit-report files to appear. Kept small
            because the polled artefacts are local files, not network
            resources.
    """

    model_config = ConfigDict(extra="forbid")

    max_iter: int = Field(default=3, ge=1)
    audit_backoff_s: int = Field(default=5, ge=1)


class OrchestratorConfig(BaseModel):
    """Defaults aligned with Wave-4 capacity expectations (8 parallel parcels).

    ``auto_land`` is the feature flag for W11's auto-land loop. It defaults
    to ``False`` — operators opt in per deployment once W15's regression
    gate is in place. Flipping it on without W15 means a parcel that
    passes its own scoped tests can still silently land while breaking
    something elsewhere.
    """

    model_config = ConfigDict(extra="forbid")

    db_path: Path = Field(default=Path(".data/orchestrator.db"))
    backend: Literal["local", "managed_agents"] = "local"
    lease_ttl_s: int = 1800
    tick_interval_s: int = 5
    max_concurrent: int = 8
    shutdown_grace_s: int = 30
    worktrees_root: Path | None = None
    claude_profile: str | None = Field(
        default=None,
        description=(
            "Name of the claude CLI profile (``~/.claude-profiles/<name>``) "
            "used by spawned parcel sessions. Set to None to fall back "
            "to the default ``~/.claude`` profile. Override per-invocation "
            "with env var ``CLAUDE_FLEET_PROFILE``, or bypass profile "
            "selection entirely with ``CLAUDE_FLEET_CONFIG_DIR`` (absolute "
            "path to any directory containing ``.credentials.json``)."
        ),
    )
    auto_land: bool = False
    auto_land_timeout_s: int = 1800
    auto_land_test_cmd: str = "uv run pytest tests/"

    # W21 auto-retire: remove landed-and-idle parcel worktrees via
    # ``git worktree remove`` after a cooldown. Defaults OFF for the same
    # reason ``auto_land`` does — operator opts in per deployment once
    # they're comfortable the regression gate catches mis-lands. One
    # retirement per tick bounds blast radius; even a runaway daemon
    # only amputates at most ``auto_retire_batch_size`` worktrees per
    # ``tick_interval_s`` seconds.
    auto_retire: bool = False
    auto_retire_idle_minutes: int = 60
    auto_retire_batch_size: int = 1

    # G2: when True (default), auto-retire only fires for jobs in the
    # ``landed`` terminal state. Setting False would allow retiring other
    # terminal states (failed, blocked) — kept disabled for safety; the
    # daemon enforces this at the call site regardless of backend config.
    retire_only_after_landed: bool = True

    # G1: worktree_base is the root directory that holds per-job worktree
    # sub-directories.  Defaults to ``./worktrees/`` relative to the CWD.
    worktree_base: Path = Field(default=Path("./worktrees"))

    # G8: minimum age (in hours) before a terminal-status worktree is
    # considered an orphan eligible for ``worktree sweep``.  Default 168 h
    # (7 days) gives operators a full week to inspect landed worktrees
    # before sweep removes them.
    orphan_age_hours: float = Field(default=168.0, gt=0)

    # G4: which verdict adapter to use when harvesting worker completions.
    # Registered built-ins: marker-file | exit-code | json-result.
    # Override per-parcel via frontmatter ``verdict_adapter:`` key.
    verdict_adapter: str = "marker-file"

    # G5: claude-lb integration — health-aware profile selection.
    # Disabled by default; set claude_lb.enabled=true to activate.
    claude_lb: ClaudeLbConfig = Field(default_factory=ClaudeLbConfig)

    # R2-5 audit-loop knobs (absorbs scripts/auto-feedback-loop.sh).
    audit_loop: AuditLoopConfig = Field(default_factory=AuditLoopConfig)


__all__ = ["AuditLoopConfig", "ClaudeLbConfig", "OrchestratorConfig"]
