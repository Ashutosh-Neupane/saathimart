import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, nowdate


class Offer(Document):
    def before_save(self):
        if not self.slug:
            self.slug = frappe.scrub(self.title).replace("_", "-")
        self._validate_linked_coupon()

    def _validate_linked_coupon(self):
        """A published Offer must not advertise a coupon code that doesn't
        exist or has already expired — the storefront surfaces this code as
        clickable, and a dead code at checkout is the exact preview/checkout
        drift class of bug seen on the sibling middleware app. Draft/Archived
        offers are still validated (fail early, at save time, not at render
        time), but an expired coupon on an archived offer is tolerated since
        it can no longer be advertised anywhere.
        """
        if not self.coupon_code:
            return
        if not frappe.db.exists("Coupon", self.coupon_code):
            frappe.throw(
                _("Coupon Code {0} does not exist").format(self.coupon_code)
            )
        valid_to = frappe.db.get_value("Coupon", self.coupon_code, "valid_to")
        if valid_to and getdate(valid_to) < getdate(nowdate()):
            if self.status == "Archived":
                return
            frappe.throw(
                _("Coupon Code {0} expired on {1} — pick an active coupon or archive this offer")
                .format(self.coupon_code, valid_to)
            )

    def on_update(self):
        frappe.cache().delete_key("sm_offers:all")
        if self.slug:
            frappe.cache().delete_key(f"sm_offer:{self.slug}")
