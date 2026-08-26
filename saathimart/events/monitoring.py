"""
Sync health monitoring — surfaces webhook delivery failures instead of
letting them fail silently in the queue.

Two entry points:
  - get_sync_health(): whitelisted, for admin dashboards / polling
  - daily_sync_health_report(): cron (daily) — emails System Managers when
    events are dead-lettered or stuck; writes an Error Log otherwise so the
    digest itself leaves a trail.
"""
import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime


def _collect():
    """Gather the sync-health numbers shared by the endpoint and the digest."""
    day_ago = add_to_date(now_datetime(), hours=-24)

    by_status = {}
    for row in frappe.get_all(
        "Webhook Event",
        fields=["status", "count(name) as n"],
        group_by="status",
    ):
        by_status[row.status] = row.n

    recent = frappe.get_all(
        "Webhook Event",
        filters={"creation": [">=", day_ago]},
        fields=["status", "count(name) as n"],
        group_by="status",
    )
    recent_by_status = {r.status: r.n for r in recent}

    dead = frappe.get_all(
        "Webhook Event",
        filters={"status": "Dead"},
        fields=["event_type", "target_vendor", "dead_letter_reason", "creation"],
        order_by="creation desc",
        limit=20,
    )

    # Queued for over an hour means delivery is failing repeatedly — worth
    # flagging even before it exhausts max_webhook_retries and dies.
    stuck_cutoff = add_to_date(now_datetime(), hours=-1)
    stuck = frappe.get_all(
        "Webhook Event",
        filters={"status": "Queued", "modified": ["<", stuck_cutoff]},
        fields=["event_type", "target_vendor", "retry_count", "response"],
        order_by="creation asc",
        limit=20,
    )

    return {
        "totals_by_status": by_status,
        "last_24h_by_status": recent_by_status,
        "dead_events": dead,
        "stuck_queued": stuck,
    }


@frappe.whitelist()
def get_sync_health():
    """JSON snapshot for an admin dashboard."""
    return _collect()


def daily_sync_health_report():
    """
    Cron daily. Emails System Managers when anything needs eyes:
    dead-lettered events, or events stuck Queued for 1h+. Silent success is
    intentional — no news means the pipes are clear.
    """
    health = _collect()
    problems = []
    if health["dead_events"]:
        problems.append(
            _("{0} dead-lettered webhook event(s)").format(len(health["dead_events"]))
        )
    if health["stuck_queued"]:
        problems.append(
            _("{0} event(s) stuck in queue for over 1 hour").format(len(health["stuck_queued"]))
        )

    if not problems:
        frappe.log_error("Sync health: clean — no dead or stuck webhook events.", "Sync Digest")
        return

    lines = [_("SaathiMart sync health report"), ""]
    lines += [_("- " + p) for p in problems]
    if health["dead_events"]:
        lines.append("")
        lines.append(_("Dead events (latest first):"))
        for e in health["dead_events"]:
            reason = (e.dead_letter_reason or "")[:200]
            lines.append(f"  • {e.event_type} → {e.target_vendor}: {reason}")
    body = "\n".join(lines)

    recipients = [
        r.name
        for r in frappe.get_all(
            "Has Role",
            filters={"role": "System Manager", "parenttype": "User"},
            pluck="parent",
            distinct=True,
        )
        if frappe.db.get_value("User", r, "enabled")
    ]
    frappe.sendmail(recipients=recipients, subject=_("SaathiMart sync issues detected"), message=body)
    frappe.log_error(body, "Sync Digest")
