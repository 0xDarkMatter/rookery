"""Notifier ABC for out-of-band operator alerts.

The orchestrator daemon calls into a :class:`Notifier` when a job reaches
a state a human needs to know about. Concrete implementations (journal,
pigeon, email, pm2-logs) belong in downstream parcels — W11 only defines
the shape plus the one hook auto-land needs.

Every method on :class:`Notifier` is a default no-op so downstream
parcels can add hooks without breaking existing concrete implementations.
"""

from __future__ import annotations


class Notifier:
    """Operator-alert sink.

    Intentionally a concrete class (not an :class:`abc.ABC`) so downstream
    parcels can add hooks without breaking existing subclasses — every
    method has a default no-op body. Treat this as the base of the
    notifier hierarchy: concrete notifiers (W13 journal / pigeon /
    pm2-logs) inherit from it and override the hooks they care about.
    """

    def merge_blocked(
        self,
        job_id: str,
        reason: str,
        detail: str | None = None,
    ) -> None:
        """Called when an auto-land attempt fails terminally.

        Default: no-op. Downstream notifiers (W13 journal / pigeon /
        pm2-logs) may override to surface the block to a human.

        Args:
            job_id: Parcel id that ended at ``merge-blocked``.
            reason: One of ``rebase-conflict``, ``tests-failed``,
                ``non-ff``, ``timeout``, ``other``.
            detail: Free-text clarifier — conflict file list, failing test
                name, main-tip sha, or the underlying error message.
        """

        return None

    def fix_exhausted(
        self,
        parcel_id: str,
        final_verdict: str,
        iter_num: int,
    ) -> None:
        """Called when an :class:`AuditLoop` hits ``max_iter`` still failing.

        Default: no-op. Downstream notifiers surface the stuck parcel to
        an operator — the audit-fix cycle has run its budget and the
        parcel needs a human's attention.

        Args:
            parcel_id: Parcel id whose audit-fix loop gave up.
            final_verdict: The last audit's verdict — currently always
                ``"BLOCK"`` (PASS / PASS_WITH_WARNINGS exit before
                exhaustion), but kept flexible for future policy changes.
            iter_num: The final iteration reached (equals ``max_iter``).
        """

        return None


class NullNotifier(Notifier):
    """Explicit no-op notifier for tests and orchestrator bootstrapping.

    Functionally identical to ``Notifier()``; kept as a named class so the
    intent (``notifier=NullNotifier()``) reads unambiguously at call sites.
    """


__all__ = ["NullNotifier", "Notifier"]
