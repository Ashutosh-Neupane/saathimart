"""Vendor routing hygiene.

Routing (``select_best_vendor`` and every customer-facing listing query)
only considers **Active** vendors, and a vendor without a ``frappe_site_url``
cannot receive order events at all — an order routed to one is silently
dropped (no vendor order, no books). This module enforces the invariant from
the data side so a stray test vendor or an unapproved listing can never leak
into the shopfront:

- ``suspend_unroutable_vendors`` (daily): flips every **Active** Vendor that
  has no ``frappe_site_url`` to ``Suspended`` — it can never receive order
  events, so an Active status is a lie that routing believes.
- ``deactivate_listings_of_inactive_vendors`` (daily): flips every Active
  ``Vendor Listing`` whose Vendor is Pending/Suspended to ``Inactive``.
- ``report_unroutable_vendors``: Active vendors missing ``frappe_site_url`` —
  they pass the Active gate but can never receive events.

Both are safe to run manually:
``bench --site saathimart.localhost execute saathimart.api.vendor_hygiene.deactivate_listings_of_inactive_vendors``
"""

import frappe
from frappe.utils import now_datetime

from saathimart.api.storefront_cache import bump_list_version


def suspend_unroutable_vendors():
    """Suspend Active vendors with no frappe_site_url (they can't receive events)."""
    modified = now_datetime()
    user = frappe.session.user if frappe.session and frappe.session.user else "Administrator"

    unroutable = frappe.db.sql(
        """SELECT name FROM `tabVendor`
           WHERE status = 'Active'
             AND (frappe_site_url IS NULL OR frappe_site_url = '')""",
        as_dict=True,
    )
    names = [r.name for r in unroutable]
    if not names:
        return {"suspended": 0}

    frappe.db.sql(
        """UPDATE `tabVendor`
           SET status = 'Suspended', modified = %s, modified_by = %s
           WHERE name IN %s""",
        (modified, user, tuple(names)),
    )
    frappe.db.commit()
    # Raw SQL bypasses doc events — bump the storefront version explicitly so
    # cached list pages don't keep routing to the now-Suspended vendors.
    bump_list_version()
    frappe.logger("vendor_hygiene").warning(
        {"suspended_unroutable_vendors": len(names), "vendors": sorted(names)}
    )
    return {"suspended": len(names), "vendors": sorted(names)}


def deactivate_listings_of_inactive_vendors():
    """Deactivate listings owned by vendors that are not Active."""
    modified = now_datetime()
    user = frappe.session.user if frappe.session and frappe.session.user else "Administrator"

    rows = frappe.get_all(
        "Vendor Listing",
        filters={"status": "Active"},
        fields=["name", "vendor"],
        limit=0,
    )
    if not rows:
        return {"deactivated": 0}

    vendors = {r.vendor for r in rows if r.vendor}
    inactive = set(
        frappe.get_all(
            "Vendor",
            filters={"name": ["in", list(vendors)], "status": ["!=", "Active"]},
            pluck="name",
        )
    )
    if not inactive:
        return {"deactivated": 0}

    stale = [r.name for r in rows if r.vendor in inactive]
    frappe.db.sql(
        """UPDATE `tabVendor Listing`
           SET status = 'Inactive', modified = %s, modified_by = %s
           WHERE name IN %s""",
        (modified, user, tuple(stale)),
    )
    frappe.db.commit()
    # Raw SQL bypasses doc events — bump the storefront version explicitly.
    bump_list_version()

    frappe.logger("vendor_hygiene").info(
        {"deactivated_listings": len(stale), "vendors": sorted(inactive)}
    )
    return {"deactivated": len(stale), "vendors": sorted(inactive)}


def report_unroutable_vendors():
    """Active vendors that can never receive order events (no site URL)."""
    rows = frappe.db.sql(
        """SELECT name, vendor_name, COALESCE(frappe_site_url, '') AS site_url
           FROM `tabVendor`
           WHERE status = 'Active'
             AND (frappe_site_url IS NULL OR frappe_site_url = '')""",
        as_dict=True,
    )
    if rows:
        frappe.logger("vendor_hygiene").warning(
            {"unroutable_active_vendors": [r.name for r in rows]}
        )
    return rows
