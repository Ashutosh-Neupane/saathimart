"""Platform-wide commission helpers.

Commission belongs to the PLATFORM (SaathiMart Settings), not to individual
vendors — one franchise-wide rate the platform charges every vendor. Kept in
one place so accounting, payouts, and reports all read the same number, and
so a future per-vendor override slot can be added without touching callers
again (this module is the single seam).
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

_CACHE_KEY = "sm_default_commission_pct"


def get_default_commission_pct() -> float:
	"""Platform commission % applied to every vendor (SaathiMart Settings).

	Cached per-request lifetime in frappe.local flags — settings rarely change
	and this is read on every order accounting + payout computation.
	"""
	if getattr(frappe.local, "sm_commission_pct", None) is not None:
		return frappe.local.sm_commission_pct
	pct = flt(frappe.db.get_single_value("SaathiMart Settings", "default_commission_pct") or 0)
	frappe.local.sm_commission_pct = pct
	return pct


def get_commission_pct_for_vendor(vendor: str | None) -> float:
	"""Commission % for a vendor. Currently the platform-wide rate for every
	vendor; a per-vendor override can slot in here later without changing
	any caller (several tests and the reconciliation report already treat
	vendors as sharing one rate).
	"""
	return get_default_commission_pct()
