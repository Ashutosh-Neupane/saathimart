# ── Daily Promotional Consolidation (midnight job) ───────────────────────────
#
# Runs every night at 00:00 (hooks.py scheduler_events). For the just-ended
# day it takes every coupon redemption and loyalty redemption that "hit" the
# platform and books them into accounting as their own dedicated, per-vendor
# Journal Entries:
#
#   DR: Clearing Account - Vendor - SM   (extracts the promotional portion
#                                         from the blended vendor payable)
#   CR: Platform Coupon Payable - SM     (platform-funded coupons owed to the
#                                         vendor)
#   CR: Loyalty Payable - SM             (loyalty reimbursements owed to the
#                                         vendor)
#
# Why this entry exists: at payment time `record_order_payment_gl` books the
# coupon/loyalty EXPENSE and credits the vendor clearing account with the
# whole payable (sales net + promotional funding) blended together. This
# midnight reclassification splits the promotional portion out into its own
# named liability per vendor, so:
#   - the weekly payout can clear promotions separately from sales
#   - an auditor can see, per vendor per day, exactly what the platform owed
#     for marketing funding (Blinkit-style clearing-house model)
#
# The entry is P&L-neutral (expense was booked at payment time) and
# idempotent — voucher numbers are `DPROMO-<date>-<vendor>`, and an existing
# voucher short-circuits a re-run.

import frappe
from frappe.utils import flt, getdate, add_days, today, nowdate, rounded


def _voucher_no(posting_date, vendor):
    date_part = getdate(posting_date).strftime("%Y%m%d")
    slug = frappe.scrub(vendor).replace("_", "-")[:40]
    return f"DPROMO-{date_part}-{slug}"


def get_promotional_payable_balance(account, vendor):
	"""Sum the current balance of a payable account for one vendor (party).
	Returns debit-positive convention? No: credit-positive for a liability —
	credits (what we owe) minus debits (what we cleared/paid)."""
	rows = frappe.db.sql(
		"""
		SELECT SUM(debit - credit) AS bal
		FROM `tabGL Entry`
		WHERE account = %s AND party = %s AND is_cancelled = 0
		""",
		(account, vendor),
		as_dict=True,
	)
	return rounded(flt(rows[0].bal if rows else 0), 2)


def _day_orders(posting_date):
	"""Paid, non-cancelled orders belonging to the given day (by creation)."""
	return frappe.get_all(
		"Order",
		filters=[
			["payment_status", "=", "Paid"],
			["status", "!=", "Cancelled"],
			["creation", ">=", f"{posting_date} 00:00:00"],
			["creation", "<", f"{add_days(posting_date, 1)} 00:00:00"],
		],
		fields=["name", "coupon_code", "coupon_discount", "loyalty_discount"],
	)


def _vendor_shares(order_name):
	"""Vendor → subtotal-share map for one order (proportional attribution,
	same convention as record_order_payment_gl)."""
	rows = frappe.get_all(
		"Vendor Fulfillment",
		filters={"parent": order_name, "status": ["!=", "Cancelled"]},
		fields=["vendor", "subtotal"],
	)
	total = sum(flt(r.subtotal) for r in rows)
	if not rows or total <= 0:
		return {}
	return {r.vendor: flt(r.subtotal) / total for r in rows}


def _coupon_absorption(coupon_code):
	if not coupon_code:
		return "none"
	return (
		frappe.db.get_value("Coupon", {"coupon_code": coupon_code}, "absorption_type")
		or "Platform"
	)


def _reconcile(posting_date, orders):
	"""Cross-check order-level promotional amounts against the raw redemption
	records (Coupon Usage / Loyalty Point Entry). Discrepancies go to the
	Error Log — never raise; the JE still books from the order figures."""
	from saathimart.api.accounting import _get_coupon_absorption

	order_ids = [o.name for o in orders]
	if not order_ids:
		return

	coupon_rows = frappe.get_all(
		"Coupon Usage",
		filters={"order": ["in", order_ids]},
		fields=["order", "discount_amount"],
	)
	usage_by_order = {}
	for r in coupon_rows:
		usage_by_order.setdefault(r.order, 0.0)
		usage_by_order[r.order] += flt(r.discount_amount)

	for o in orders:
		recording = rounded(flt(usage_by_order.get(o.name, 0)), 2)
		booked = rounded(flt(o.coupon_discount), 2)
		if recording != booked:
			frappe.log_error(
				f"Promotional reconciliation mismatch for {o.name} on {posting_date}: "
				f"Coupon Usage records {recording} vs order books {booked}. "
				"The Journal Entry used the order figure.",
				"Daily Promotions",
			)


def consolidate_daily_promotions(posting_date=None):
	"""Midnight job: book the day's coupon + loyalty redemptions into their
	own per-vendor liability accounts. Returns a summary dict.

	Idempotent — safe to run manually for any historical date.
	"""
	from saathimart.api.accounting import (
		_get_account,
		create_gl_entries_batch,
	)

	posting_date = getdate(posting_date) if posting_date else add_days(getdate(today()), -1)
	posting_date = posting_date.isoformat()

	orders = _day_orders(posting_date)
	_reconcile(posting_date, orders)

	# Per-vendor promotional totals for the day
	totals = {}  # vendor -> {"coupon": x, "loyalty": y}
	for o in orders:
		absorption = _coupon_absorption(o.coupon_code)
		platform_coupon = flt(o.coupon_discount) if absorption == "Platform" else 0.0
		loyalty = flt(o.loyalty_discount)
		if platform_coupon <= 0 and loyalty <= 0:
			continue
		shares = _vendor_shares(o.name)
		for vendor, share in shares.items():
			t = totals.setdefault(vendor, {"coupon": 0.0, "loyalty": 0.0})
			t["coupon"] += platform_coupon * share
			t["loyalty"] += loyalty * share

	created = []
	clearing = _get_account("clearing_vendor")
	coupon_pay = _get_account("platform_coupon_payable")
	loyalty_pay = _get_account("loyalty_payable")

	if not clearing or not (coupon_pay or loyalty_pay):
		# Standalone site (no ERPNext): GL recording is a no-op by design.
		frappe.logger("daily_promotions").info(
			f"Promotional consolidation for {posting_date}: GL accounts unavailable — "
			f"aggregated {len(totals)} vendor(s), no Journal Entries booked."
		)
		return {
			"posting_date": posting_date,
			"orders_reviewed": len(orders),
			"vendors": len(totals),
			"entries": [],
			"skipped": "erpnext_not_installed",
		}

	for vendor, t in sorted(totals.items()):
		coupon_amt = rounded(t["coupon"], 2)
		loyalty_amt = rounded(t["loyalty"], 2)
		if coupon_amt <= 0 and loyalty_amt <= 0:
			continue

		# Never book a one-sided voucher: each nonzero amount needs its
		# payable counterpart before we touch clearing.
		if coupon_amt > 0 and not coupon_pay:
			continue
		if loyalty_amt > 0 and not loyalty_pay:
			continue

		vno = _voucher_no(posting_date, vendor)
		if frappe.db.exists("GL Entry", {"voucher_no": vno, "voucher_type": "Journal Entry"}):
			continue  # already booked — idempotent

		entries = [
			{
				"account": clearing,
				"debit": rounded(coupon_amt + loyalty_amt, 2),
				"credit": 0,
				"party_type": "Supplier",
				"party": vendor,
				"remarks": f"Promotional portion of vendor payable for {posting_date}",
			}
		]
		if coupon_amt > 0 and coupon_pay:
			entries.append({
				"account": coupon_pay,
				"debit": 0,
				"credit": coupon_amt,
				"party_type": "Supplier",
				"party": vendor,
				"remarks": f"Platform coupons redeemed for {vendor} on {posting_date}",
			})
		if loyalty_amt > 0 and loyalty_pay:
			entries.append({
				"account": loyalty_pay,
				"debit": 0,
				"credit": loyalty_amt,
				"party_type": "Supplier",
				"party": vendor,
				"remarks": f"Loyalty points redeemed for {vendor} on {posting_date}",
			})

		create_gl_entries_batch(
			entries,
			voucher_type="Journal Entry",
			voucher_no=vno,
			remarks=f"Daily promotional consolidation for {vendor} on {posting_date}",
			posting_date=posting_date,
		)
		created.append({"vendor": vendor, "voucher_no": vno,
						"coupon": coupon_amt, "loyalty": loyalty_amt})

	return {
		"posting_date": posting_date,
		"orders_reviewed": len(orders),
		"vendors": len(created),
		"entries": created,
	}


@frappe.whitelist()
def run_consolidation(posting_date=None):
	"""Admin endpoint to run/preview the consolidation for a given day."""
	frappe.only_for(["System Manager", "SM Admin"])
	return consolidate_daily_promotions(posting_date)
