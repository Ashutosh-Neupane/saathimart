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
	"""Commission % for a vendor: the vendor's own contract rate from the
	hub Vendor row (Vendor.commission_pct, set per vendor on the hub desk),
	falling back to the platform-wide SaathiMart Settings rate when the
	vendor has no explicit override. Reads hub-local data only — safe from
	any hub process (accounting, payouts, reports, settlement push).
	"""
	if vendor:
		pct = frappe.db.get_value("Vendor", vendor, "commission_pct")
		if pct is not None and flt(pct) > 0:
			_stamp_effective_rate_note(vendor, flt(pct), "vendor")
			return flt(pct)
	default = get_default_commission_pct()
	if vendor:
		_stamp_effective_rate_note(vendor, default, "default")
	return default


def _stamp_effective_rate_note(vendor: str, pct: float, source: str) -> None:
	"""Show on the Vendor form WHICH rate source won, so an admin never has to
	guess whether a payout used the vendor contract or the platform default.
	Written only when the provenance actually changes (hot path — this runs on
	every order calculation); a failed stamp must never break accounting.
	"""
	note = f"{pct:g}% ({'vendor contract rate' if source == 'vendor' else 'platform default'})"
	try:
		if frappe.db.get_value("Vendor", vendor, "commission_effective_note") != note:
			frappe.db.set_value("Vendor", vendor, "commission_effective_note",
								note, update_modified=False)
	except Exception:
		frappe.log_error(f"commission note stamp failed for {vendor}", "Commission")


def get_platform_ledger_vendor() -> str | None:
	"""The one Vendor whose ERPNext site keeps SaathiMart's own books.

	The hub runs plain Frappe with no ERPNext, so it cannot hold GL Entries
	itself (see accounting.py's module docstring) — Entity A's accounting
	(commission income, platform coupon/loyalty expense, delivery income,
	TDS receivable) is pushed as platform.* events to this vendor's site
	instead, the same way settlement/order events already go to any vendor.
	Returns None (callers should log and skip, not throw) until an admin
	sets SaathiMart Settings > Platform Ledger Vendor.
	"""
	if getattr(frappe.local, "sm_platform_ledger_vendor", None) is not None:
		return frappe.local.sm_platform_ledger_vendor or None
	vendor = frappe.db.get_single_value("SaathiMart Settings", "platform_ledger_vendor") or ""
	frappe.local.sm_platform_ledger_vendor = vendor
	return vendor or None
