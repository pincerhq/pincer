"""
Alert rules over the golden signals (Sprint 9, T9.2).

Two severities, because they mean different things to the person receiving them:

``PAGE``    Something is broken *right now* and money or a customer call is
            burning. Stuck calls, the service being down, `doctor` going RED
            after a deploy, the disk filling up. Delivered immediately, every
            time, no suppression window beyond de-duplication.
``NOTIFY``  A threshold has drifted. Worth looking at today, not at 03:00.
            Repeat-suppressed to `ops_alert_repeat_min`.

Design decisions worth knowing before changing anything here:

* **Insufficient data never fires.** Every rule declares a minimum sample size;
  one failed call out of one is not an 85%-success-rate violation. An alert
  system that cries wolf gets muted, and a muted pager is worse than none.
* **Recovery is announced.** A rule that fired and then clears sends a
  ``RESOLVED`` message. Without it, an operator cannot tell "fixed" from
  "still broken but I stopped being told".
* **Delivery failure is itself loud.** If the ops channel cannot be reached the
  alert is logged at ERROR and, when configured, emailed — an alerting system
  that silently fails to deliver is indistinguishable from a healthy system.
* **State is in-process.** Suppression windows reset on restart, which means a
  restart can re-announce a firing alert. That is the safe direction to fail.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.config import Settings
    from pincer.observability.golden_signals import GoldenSignals, Signal

logger = logging.getLogger(__name__)


class Severity(StrEnum):
    PAGE = "page"
    NOTIFY = "notify"


@dataclass
class Alert:
    rule: str
    severity: Severity
    title: str
    detail: str
    value: float | None = None
    threshold: float | None = None
    runbook: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        icon = "🚨" if self.severity is Severity.PAGE else "⚠️"
        lines = [f"{icon} *{self.title}*", self.detail]
        if self.value is not None and self.threshold is not None:
            lines.append(f"Value: {self.value:.3g} (threshold {self.threshold:.3g})")
        if self.runbook:
            lines.append(f"Runbook: {self.runbook}")
        return "\n".join(lines)

    def render_resolved(self) -> str:
        return f"✅ *RESOLVED: {self.title}*\n{self.detail}"


# Runbook anchors — the alert tells you where the fix is written down, so an
# operator never has to guess which section applies at 03:00.
RUNBOOK = "docs/operations/runbook.md"


def _anchor(section: str) -> str:
    return f"{RUNBOOK}#{section}"


# ── Rule evaluation ──────────────────────────────────────────────────


def _breached(signal: Signal, *, lower_is_bad: bool) -> bool:
    """True when the signal has enough data AND has crossed its threshold.

    The sample-size guard lives here rather than in each rule so no rule can
    forget it: firing "success rate 0%" on a single failed call is how an
    alerting system gets muted.
    """
    if not signal.sufficient_data or signal.value is None or signal.target is None:
        return False
    return signal.value < signal.target if lower_is_bad else signal.value > signal.target


def evaluate(signals: GoldenSignals, settings: Settings | Any) -> list[Alert]:
    """Every alert currently firing, most severe first."""
    alerts: list[Alert] = []

    # 1. Stuck calls — pages immediately. Every second one survives costs money
    #    and holds a line open, so there is no threshold and no window.
    stuck = signals.stuck_calls
    if stuck.value:
        detail = ", ".join(
            f"{c['call_sid']} ({c['duration_seconds']}s, {c['over_by_seconds']}s over)"
            for c in stuck.detail.get("stuck", [])
        )
        alerts.append(
            Alert(
                rule="stuck_calls",
                severity=Severity.PAGE,
                title=f"{int(stuck.value)} stuck call(s)",
                detail=f"Active past {stuck.detail.get('threshold_seconds')}s: {detail}",
                value=stuck.value,
                threshold=0.0,
                runbook=_anchor("stuck-call"),
                context=stuck.detail,
            )
        )

    # 2. Call success rate.
    success = signals.call_success_rate
    if _breached(success, lower_is_bad=True):
        worst = sorted((success.detail.get("by_failure_code") or {}).items(), key=lambda kv: kv[1], reverse=True)
        top = ", ".join(f"{code}×{count}" for code, count in worst[:4] if code != "none")
        alerts.append(
            Alert(
                rule="call_success_rate",
                severity=Severity.NOTIFY,
                title=f"Call success rate {success.value:.0%} over {success.window}",  # type: ignore[union-attr]
                detail=(
                    f"{success.detail.get('completed')}/{success.detail.get('terminated')} calls completed."
                    + (f" Top failures: {top}." if top else "")
                ),
                value=success.value,
                threshold=success.target,
                runbook=_anchor("call-success-rate-drop"),
                context=success.detail,
            )
        )

    # 3. Booking success rate.
    booking = signals.booking_success_rate
    if _breached(booking, lower_is_bad=True):
        alerts.append(
            Alert(
                rule="booking_success_rate",
                severity=Severity.NOTIFY,
                title=f"Booking success rate {booking.value:.0%} over {booking.window}",  # type: ignore[union-attr]
                detail=(
                    f"{booking.detail.get('confirmed')}/{booking.detail.get('cooperative_attempts')} "
                    f"cooperative appointment calls reached a confirmed slot. "
                    f"Breakdown: {booking.detail.get('by_result')}"
                ),
                value=booking.value,
                threshold=booking.target,
                runbook=_anchor("booking-success-rate-drop"),
                context=booking.detail,
            )
        )

    # 4. Turn latency p95.
    latency = signals.turn_latency
    if _breached(latency, lower_is_bad=False):
        alerts.append(
            Alert(
                rule="turn_latency_p95",
                severity=Severity.NOTIFY,
                title=f"Voice latency p95 {latency.value:.2f}s over {latency.window}",  # type: ignore[union-attr]
                detail=(
                    f"p50 {latency.detail.get('p50_s')}s / p95 {latency.detail.get('p95_s')}s "
                    f"across {latency.sample_size} turns (SLO p95 ≤ {latency.detail.get('slo_p95_s')}s). "
                    "Run `pincer voice latency-report` to see which stage regressed."
                ),
                value=latency.value,
                threshold=latency.target,
                runbook=_anchor("latency-regression"),
                context=latency.detail,
            )
        )

    # 5. Cost per call, relative to its own baseline.
    cost = signals.cost_per_call
    if _breached(cost, lower_is_bad=False):
        baseline_label = cost.window.split(" vs ")[-1]
        alerts.append(
            Alert(
                rule="cost_per_call",
                severity=Severity.NOTIFY,
                title=f"Cost per call {cost.value:.1f}× the {baseline_label} baseline",  # type: ignore[union-attr]
                detail=(
                    f"p95 ${cost.detail.get('recent_p95_usd')} vs baseline "
                    f"${cost.detail.get('baseline_p95_usd')} over {cost.detail.get('calls_in_window')} calls."
                ),
                value=cost.value,
                threshold=cost.target,
                runbook=_anchor("cost-spike"),
                context=cost.detail,
            )
        )

    # 6. Sprint 12 §10.3: inbound capacity — callers hearing "all lines busy"
    #    more than 5 times a day means the line is under-provisioned.
    busy = getattr(signals, "busy_capacity", None)
    if busy is not None and busy.value is not None and busy.target is not None and busy.value > busy.target:
        alerts.append(
            Alert(
                rule="busy_capacity",
                severity=Severity.NOTIFY,
                title=f"{int(busy.value)} inbound call(s) declined for capacity in {busy.window}",
                detail=(
                    f"PINCER_INBOUND_MAX_CONCURRENT was reached {int(busy.value)} times "
                    f"(threshold {int(busy.target)}/day). Raise the limit or add capacity."
                ),
                value=busy.value,
                threshold=busy.target,
                runbook=_anchor("busy-capacity"),
                context=busy.detail,
            )
        )

    # 7. Callers who seemed unhappy. Not a system failure — which is exactly
    #    why it notifies rather than pages: it is a prompt to go listen, and
    #    the rationale for each call is in its analytics record.
    negative = getattr(signals, "negative_sentiment", None)
    if (
        negative is not None
        and negative.value is not None
        and negative.target is not None
        and negative.value >= negative.target
    ):
        alerts.append(
            Alert(
                rule="negative_sentiment",
                severity=Severity.NOTIFY,
                title=f"{int(negative.value)} call(s) with a dissatisfied caller in {negative.window}",
                detail=(
                    f"{int(negative.value)} call(s) were read as negative "
                    f"(threshold {int(negative.target)}/day). Open their transcripts — the sentiment "
                    "rationale on each call says what the reading was based on."
                ),
                value=negative.value,
                threshold=negative.target,
                runbook=_anchor("negative-sentiment"),
                context=negative.detail,
            )
        )

    alerts.sort(key=lambda a: 0 if a.severity is Severity.PAGE else 1)
    return alerts


# ── Host checks (T9.2: "host basics — disk for SQLite/backups!") ─────


def disk_alert(settings: Settings | Any) -> Alert | None:
    """Page before the data volume fills.

    A full disk is the failure that takes everything with it at once: SQLite
    writes fail, so transcripts, cost records, and the do-not-call list all stop
    persisting — and the backup that would let you recover cannot be written
    either.
    """
    threshold_pct = float(getattr(settings, "alert_disk_free_min_pct", 10.0))
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is None:
        return None
    try:
        usage = shutil.disk_usage(str(data_dir))
    except OSError:
        logger.warning("Could not read disk usage for %s", data_dir)
        return None

    free_pct = (usage.free / usage.total * 100.0) if usage.total else 0.0
    if free_pct >= threshold_pct:
        return None
    return Alert(
        rule="disk_space",
        severity=Severity.PAGE,
        title=f"Disk {free_pct:.1f}% free on the data volume",
        detail=(
            f"{usage.free / 1e9:.1f} GB free of {usage.total / 1e9:.1f} GB at {data_dir}. "
            "SQLite writes and backups share this volume — both fail when it fills."
        ),
        value=free_pct,
        threshold=threshold_pct,
        runbook=_anchor("disk-full"),
        context={"free_bytes": usage.free, "total_bytes": usage.total, "path": str(data_dir)},
    )


def doctor_alert(settings: Settings | Any) -> Alert | None:
    """Page when the production security/config gate is RED.

    Run after a deploy: a CRITICAL here means the instance is live in a state
    the deploy gate would have refused.
    """
    try:
        from pincer.security.doctor import CheckStatus, SecurityDoctor

        report = SecurityDoctor(production=True).run_all()
    except Exception:
        logger.exception("Doctor alert check failed")
        return None

    critical = [c for c in report.checks if c.status == CheckStatus.CRITICAL]
    if not critical:
        return None
    return Alert(
        rule="doctor_red",
        severity=Severity.PAGE,
        title=f"pincer doctor --production: {len(critical)} CRITICAL",
        detail="; ".join(f"{c.name}: {c.message}" for c in critical[:5]),
        value=float(len(critical)),
        threshold=0.0,
        runbook=_anchor("doctor-red-after-deploy"),
        context={"critical": [c.name for c in critical]},
    )


# ── Delivery ─────────────────────────────────────────────────────────

# rule -> last delivery timestamp (monotonic). In-process by design: a restart
# re-announcing a still-firing alert is the safe failure direction.
_last_sent: dict[str, float] = {}
_firing: set[str] = set()


def reset_for_tests() -> None:
    _last_sent.clear()
    _firing.clear()


def _should_send(alert: Alert, repeat_after_s: float, now: float) -> bool:
    """Pages always go out; notifications are repeat-suppressed."""
    if alert.severity is Severity.PAGE:
        last = _last_sent.get(alert.rule)
        # Even a page is de-duplicated within one scan interval, or a stuck call
        # would send a message every few seconds until someone kills it.
        return last is None or (now - last) >= min(repeat_after_s, 300.0)
    last = _last_sent.get(alert.rule)
    return last is None or (now - last) >= repeat_after_s


async def deliver(settings: Settings | Any, alerts: list[Alert]) -> list[Alert]:
    """Send the alerts that are due, announce recoveries, return what was sent."""
    if not getattr(settings, "ops_alerts_enabled", True):
        return []

    from pincer.observability.metrics import record_alert_fired

    now = time.monotonic()
    repeat_after_s = float(getattr(settings, "ops_alert_repeat_min", 60)) * 60.0
    firing_now = {a.rule for a in alerts}

    # Recoveries first: an operator wants "it's fixed" before the next warning.
    for rule in sorted(_firing - firing_now):
        _last_sent.pop(rule, None)
        await _send(settings, f"✅ *RESOLVED: {rule}*\nThe condition cleared.", Severity.NOTIFY)
    _firing.clear()
    _firing.update(firing_now)

    sent: list[Alert] = []
    for alert in alerts:
        record_alert_fired(rule=alert.rule, severity=str(alert.severity))
        if not _should_send(alert, repeat_after_s, now):
            logger.debug("Alert %s suppressed (repeat window)", alert.rule)
            continue
        await _send(settings, alert.render(), alert.severity)
        _last_sent[alert.rule] = now
        sent.append(alert)
    return sent


async def _send(settings: Settings | Any, text: str, severity: Severity) -> None:
    """Deliver to the ops channel; fall back to email, then to a loud log."""
    user_id = str(getattr(settings, "ops_user_id", "") or getattr(settings, "default_user_id", "") or "")
    channel = str(getattr(settings, "ops_channel", "") or "")

    delivered = False
    if user_id:
        try:
            from pincer.voice.status_notify import send_user_message

            delivered = await send_user_message(user_id, channel, text)
        except Exception:
            logger.exception("Ops alert delivery to %s/%s failed", channel, user_id)

    if delivered:
        logger.info("Ops alert delivered (%s): %s", severity, text.splitlines()[0])
        return

    email = str(getattr(settings, "ops_alert_email", "") or "")
    if email:
        try:
            from pincer.tools.builtin.email_tool import email_send

            subject = f"[Pincer {severity}] {text.splitlines()[0].strip('*🚨⚠️✅ ')}"
            result = await email_send(to=email, subject=subject, body=text)
            if not str(result).lower().startswith("error"):
                logger.info("Ops alert delivered by email fallback to %s", email)
                return
            logger.error("Ops alert email fallback rejected: %s", result)
        except Exception:
            logger.exception("Ops alert email fallback to %s failed", email)

    # Last resort: an undeliverable alert must still be impossible to miss in
    # the logs, because a silent alerting system looks exactly like a healthy one.
    logger.error("OPS ALERT UNDELIVERED (%s):\n%s", severity, text)


async def scan(settings: Settings | Any) -> list[Alert]:
    """One full evaluation pass: golden signals + host checks, then deliver.

    This is what the scheduled `ops_alert_scan` action calls.
    """
    from pincer.observability import golden_signals as gs

    signals = await gs.collect(settings)
    alerts = evaluate(signals, settings)

    for host_check in (disk_alert(settings),):
        if host_check is not None:
            alerts.insert(0, host_check)

    return await deliver(settings, alerts)


def make_alert_scan_handler(settings: Settings | Any) -> Any:
    """Build the CronScheduler action handler for ``ops_alert_scan``.

    Returns None on a clean scan: the handler's return value is delivered to
    the ops channel, and alerts already deliver themselves through `_send`.
    Returning a summary here would double-send every alert and post an
    "all clear" every few minutes.
    """

    async def _handler(pincer_user_id: str, action: dict[str, Any], channel: str) -> str | None:
        try:
            await scan(settings)
        except Exception:
            logger.exception("Ops alert scan failed")
        return None

    return _handler
