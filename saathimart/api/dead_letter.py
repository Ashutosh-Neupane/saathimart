"""
Dead letter auto-recovery — retries failed webhook events, archives old ones,
and alerts admins when dead-letter count exceeds threshold.

Three entry points:
  - retry_dead_letters(): daily cron — retries Dead events with reset backoff
  - archive_old_events(): weekly cron — archives events older than 7 days
  - dead_letter_alert(): daily cron — alerts when Dead count > threshold
"""
import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime


MAX_RETRY_AGE_DAYS = 7     # don't retry events older than this
ARCHIVE_AGE_DAYS = 30      # archive events older than this
ALERT_THRESHOLD = 10       # alert when Dead count exceeds this


def retry_dead_letters():
    """Daily cron: retry Dead events that are still within the retry window."""
    cutoff = add_to_date(now_datetime(), days=-MAX_RETRY_AGE_DAYS)

    dead_events = frappe.get_all(
        "Webhook Event",
        filters={
            "status": "Dead",
            "creation": (">=", cutoff),
            "target_site": ["!=", ""],
        },
        fields=["name", "event_type", "target_vendor", "creation"],
        limit=50,
    )

    return _requeue(dead_events)


def _requeue(dead_events):
    """Shared requeue: reset retry bookkeeping, then kick the delivery queue."""
    retried = 0
    for evt in dead_events:
        try:
            frappe.db.set_value("Webhook Event", evt.name, {
                "status": "Queued",
                "retry_count": 0,
                "next_retry_at": None,
                "dead_letter_reason": None,
            })
            retried += 1
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Dead letter retry failed for {evt.name}",
            )

    if retried:
        frappe.db.commit()
        # Delivery workers pick up Queued events from drain_event_queue;
        # enqueue it now so the replay doesn't wait for the next scheduler
        # tick to actually deliver.
        try:
            frappe.enqueue(
                "saathimart.events.publisher.drain_event_queue", queue="short"
            )
        except Exception:
            # Scheduler drains on its own schedule regardless; delivery is
            # never lost, just possibly delayed to the next tick.
            frappe.logger("dead_letter").info(
                "drain_event_queue enqueue failed; scheduler will pick up"
            )
        frappe.logger("dead_letter").info(f"Retried {retried} dead-letter events")

    return retried


@frappe.whitelist()
def replay_dead_letters(limit=100):
    """Desk action behind the Ops Dashboard dead-letter Replay button.

    Requeues Dead events within the retry window (oldest first) and returns
    what happened so the UI can toast a precise result. Permission-gated to
    the same roles the Ops Dashboard page itself is limited to.
    """
    if not frappe.has_permission("Webhook Event", "write"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    cutoff = add_to_date(now_datetime(), days=-MAX_RETRY_AGE_DAYS)
    dead_events = frappe.get_all(
        "Webhook Event",
        filters={
            "status": "Dead",
            "creation": (">=", cutoff),
            "target_site": ["!=", ""],
        },
        fields=["name"],
        order_by="creation asc",
        limit=cint(limit) or 100,
    )

    retried = _requeue(dead_events)
    remaining = frappe.db.count("Webhook Event", {"status": "Dead"})
    return {
        "requeued": retried,
        "remaining_dead": remaining,
        "message": _("{0} events requeued for delivery").format(retried)
        if retried
        else _("Nothing to replay"),
    }


def archive_old_events():
    """Weekly cron: archive events older than ARCHIVE_AGE_DAYS."""
    cutoff = add_to_date(now_datetime(), days=-ARCHIVE_AGE_DAYS)

    # Delete old Sent events (successfully delivered, no longer needed)
    deleted = frappe.db.sql("""
        DELETE FROM `tabWebhook Event`
        WHERE status = 'Sent' AND creation < %s
        LIMIT 1000
    """, (cutoff,))

    # Delete old Dead events (exhausted retries, archived)
    deleted_dead = frappe.db.sql("""
        DELETE FROM `tabWebhook Event`
        WHERE status = 'Dead' AND creation < %s
        LIMIT 1000
    """, (cutoff,))

    frappe.db.commit()

    total = (deleted or 0) + (deleted_dead or 0)
    if total:
        frappe.logger("dead_letter").info(f"Archived {total} old events")


def dead_letter_alert():
    """Daily cron: alert admins when Dead count exceeds threshold."""
    dead_count = frappe.db.count("Webhook Event", {"status": "Dead"})

    if dead_count <= ALERT_THRESHOLD:
        return

    # Get breakdown by vendor
    breakdown = frappe.db.sql("""
        SELECT target_vendor, COUNT(*) as cnt
        FROM `tabWebhook Event`
        WHERE status = 'Dead'
        GROUP BY target_vendor
        ORDER BY cnt DESC
    """, as_dict=True)

    lines = [f"Dead letter queue has {dead_count} events (threshold: {ALERT_THRESHOLD})"]
    for b in breakdown:
        lines.append(f"  {b.target_vendor or 'unknown'}: {b.cnt}")

    body = "\n".join(lines)

    # Email System Managers
    recipients = [
        r.name for r in frappe.get_all(
            "Has Role",
            filters={"role": "System Manager", "parenttype": "User"},
            pluck="parent",
            distinct=True,
        )
        if frappe.db.get_value("User", r, "enabled")
    ]

    if recipients:
        try:
            frappe.sendmail(
                recipients=recipients,
                subject=f"SaathiMart Dead Letter Alert — {dead_count} events",
                message="<pre>{0}</pre>".format(body),
            )
        except Exception:
            pass

    frappe.log_error(body, "Dead Letter Alert")
