import frappe
import requests


class ERPNextAPIError(Exception):
	pass

def _headers(franchise):
	return {
		"Authorization": f"token {franchise.erpnext_api_key}:{franchise.get_password('erpnext_api_secret')}",
		"Content-Type": "application/json",
	}


def _base_url(franchise):
	if not franchise.erpnext_site_url:
		frappe.throw(f"Franchise {franchise.name} has no ERPNext Site URL configured")
	return franchise.erpnext_site_url.rstrip("/")


def _request(franchise, method, path, payload=None, params=None):
	url = f"{_base_url(franchise)}{path}"
	try:
		response = requests.request(
			method,
			url,
			json=payload,
			params=params,
			headers=_headers(franchise),
			timeout=20,
		)
	except requests.RequestException as e:
		raise ERPNextAPIError(f"{method} {path} failed: {e}") from e

	if not response.ok:
		raise ERPNextAPIError(f"{method} {path} returned HTTP {response.status_code}: {response.text[:500]}")

	if not response.content:
		return {}
	return response.json()


def find_customer_by_mobile(franchise, mobile_no):
	result = _request(
		franchise,
		"GET",
		"/api/resource/Customer",
		params={
			"filters": frappe.as_json([["mobile_no", "=", mobile_no]]),
			"fields": frappe.as_json(["name"]),
			"limit_page_length": 1,
		},
	)
	rows = result.get("data") or []
	return rows[0]["name"] if rows else None


def upsert_customer(franchise, customer_name, mobile_no, email_id=None):
	existing = find_customer_by_mobile(franchise, mobile_no) if mobile_no else None
	if existing:
		return existing

	payload = {
		"doctype": "Customer",
		"customer_name": customer_name,
		"customer_type": "Individual",
		"mobile_no": mobile_no,
		"email_id": email_id,
	}
	result = _request(franchise, "POST", "/api/resource/Customer", payload=payload)
	return result["data"]["name"]


def find_address(franchise, customer, address_line, city):
	result = _request(
		franchise,
		"GET",
		"/api/resource/Address",
		params={
			"filters": frappe.as_json(
				[
					["Dynamic Link", "link_doctype", "=", "Customer"],
					["Dynamic Link", "link_name", "=", customer],
					["address_line1", "=", address_line],
					["city", "=", city],
				]
			),
			"fields": frappe.as_json(["name"]),
			"limit_page_length": 1,
		},
	)
	rows = result.get("data") or []
	return rows[0]["name"] if rows else None


def upsert_address(franchise, customer, address_line, city):
	existing = find_address(franchise, customer, address_line, city)
	if existing:
		return existing

	payload = {
		"doctype": "Address",
		"address_title": customer,
		"address_type": "Shipping",
		"address_line1": address_line,
		"city": city,
		"country": "Nepal",
		"links": [{"link_doctype": "Customer", "link_name": customer}],
	}
	result = _request(franchise, "POST", "/api/resource/Address", payload=payload)
	return result["data"]["name"]


def _get_tax_template_rows(franchise, template_name):
	# Fetches a Sales Taxes and Charges Template's own rows to merge into a transaction payload. Can't just set taxes_and_charges on the payload and let ERPNext auto-populate: its own set_taxes_and_charges() (accounts_controller.py) skips populating from the template entirely whenever the payload already has any `taxes` rows -- and we always need our own
	# delivery-charge row alongside VAT. So we replicate what ERPNext's own get_taxes_and_charges does: read the template, strip it down to the fields that matter (rate, account, whether it's tax-inclusive), and send those rows ourselves.

	result = _request(franchise, "GET", f"/api/resource/Sales Taxes and Charges Template/{template_name}")
	return [
		{
			"charge_type": row["charge_type"],
			"account_head": row["account_head"],
			"rate": row["rate"],
			"included_in_print_rate": row["included_in_print_rate"],
			"description": row["description"],
		}
		for row in result["data"]["taxes"]
	]


def create_sales_order(
	franchise, customer, address_name, items, delivery_date, delivery_charges=0, discount_amount=0
):
	so_items = [
		{
			"item_code": item["item_code"],
			"qty": item["qty"],
			"rate": item["rate"],
			"warehouse": franchise.erpnext_default_warehouse,
			"delivery_date": delivery_date,
		}
		for item in items
	]
	payload = {
		"doctype": "Sales Order",
		"customer": customer,
		"company": franchise.erpnext_company,
		"selling_price_list": franchise.erpnext_selling_price_list,
		"delivery_date": delivery_date,
		"customer_address": address_name,
		"shipping_address_name": address_name,
		"items": so_items,
	}
	if franchise.reserve_stock:
		payload["reserve_stock"] = 1

	vat_rows = []
	if franchise.erpnext_taxes_and_charges_template:
		payload["taxes_and_charges"] = franchise.erpnext_taxes_and_charges_template
		vat_rows = _get_tax_template_rows(franchise, franchise.erpnext_taxes_and_charges_template)

	vat_row = next((row for row in vat_rows if row["charge_type"] == "On Net Total"), None)
	vat_rate = vat_row["rate"] if vat_row else None

	if discount_amount:
		# discount_amount is the customer-facing, VAT-inclusive reduction (e.g. "10% off a NPR 1500 item" = NPR 150 off what the customer actually pays). apply_discount_on "Net Total" reduces the *pre-tax* net_total, and VAT is then recalculated on that
		# smaller base -- so feeding it the inclusive 150 directly would reduce net_total by 150 AND its VAT, overshooting to a ~169.50 final-price drop instead of 150. Convert to the pre-tax equivalent first so the final price drops by exactly what was promised, and CBMS gets the taxable value that actually corresponds to the price the customer was charged.
		discount_amount_pretax = discount_amount / (1 + vat_rate / 100) if vat_rate else discount_amount

		# Net Total, not Grand Total: reduces net_total (and each item's net_amount) *before* tax is calculated, so VAT ends up computed on the post-discount value -- matches Nepal VAT Act Section 12 (commercial discounts excluded from taxable value). Grand Total would risk landing on ERPNext's is_cash_or_non_trade_discount path, where the discount doesn't reduce VAT at all -- not what a coupon is.
		payload["apply_discount_on"] = "Net Total"
		payload["discount_amount"] = discount_amount_pretax

	# The template's own row(s) -- e.g. 13% VAT "On Net Total", included_in_print_rate=1 -- are sent unchanged. That's what correctly backs VAT out of each item's own inclusive rate; touching it would break that (verified live: repurposing it into a delivery-chainedrow left items untaxed instead of taxing both).
	taxes = list(vat_rows)

	if delivery_charges:
		if not franchise.erpnext_delivery_charge_account:
			frappe.throw(
				f"Franchise {franchise.name} has a delivery charge but no erpnext_delivery_charge_account configured"
			)

		# Same inclusive-to-pretax conversion as the discount above.
		delivery_charges_pretax = delivery_charges / (1 + vat_rate / 100) if vat_rate else delivery_charges

		taxes.append(
			{
				"charge_type": "Actual",
				"account_head": franchise.erpnext_delivery_charge_account,
				"description": "Delivery Charge",
				"tax_amount": delivery_charges_pretax,
			}
		)
		if vat_row:
			delivery_row_id = str(len(taxes))
			taxes.append(
				{
					"charge_type": "On Previous Row Amount",
					"account_head": vat_row["account_head"],
					"rate": vat_rate,
					"row_id": delivery_row_id,
					"description": "VAT on Delivery Charge",
				}
			)

	if taxes:
		payload["taxes"] = taxes

	result = _request(franchise, "POST", "/api/resource/Sales Order", payload=payload)
	so = result["data"]
	_request(
		franchise,
		"PUT",
		f"/api/resource/Sales Order/{so['name']}",
		payload={"docstatus": 1},
	)
	return so["name"]


def _get_mapped_doc(franchise, method, source_name):
	return _request(
		franchise,
		"GET",
		f"/api/method/{method}",
		params={"source_name": source_name},
	)["message"]


def create_paid_sales_invoice(franchise, sales_order_name, mode_of_payment, paid_amount, update_stock=1):
	# update_stock must be 0 if a Delivery Note already moved the stock for this Sales Order (the COD path) -- otherwise ERPNext rejects it as delivering the same qty twice. Only the pure-prepaid path (no Delivery Note ever created) needs update_stock=1, since there the invoice itself is what moves the stock.

	invoice = _get_mapped_doc(
		franchise, "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice", sales_order_name
	)
	invoice["is_pos"] = 1
	invoice["update_stock"] = 1 if update_stock else 0
	invoice["paid_amount"] = paid_amount
	invoice["payments"] = [{"mode_of_payment": mode_of_payment, "amount": paid_amount}]

	result = _request(franchise, "POST", "/api/resource/Sales Invoice", payload=invoice)
	invoice_name = result["data"]["name"]
	_request(franchise, "PUT", f"/api/resource/Sales Invoice/{invoice_name}", payload={"docstatus": 1})
	return invoice_name


def create_delivery_note(franchise, sales_order_name):
	delivery_note = _get_mapped_doc(
		franchise, "erpnext.selling.doctype.sales_order.sales_order.make_delivery_note", sales_order_name
	)
	result = _request(franchise, "POST", "/api/resource/Delivery Note", payload=delivery_note)
	dn_name = result["data"]["name"]
	_request(franchise, "PUT", f"/api/resource/Delivery Note/{dn_name}", payload={"docstatus": 1})
	return dn_name


def get_document_status(franchise, doctype, name):
	# Read-only poll of a single document's own status field(s), as maintained by ERPNext itself
	result = _request(franchise, "GET", f"/api/resource/{doctype}/{name}")
	return result["data"]["status"]


def find_sales_invoice_for_order(franchise, sales_order_name):
	# Finds a Sales Invoice billed against this Sales Order, regardless of who created it. Needed because a COD invoice is normally created by franchise staff directly on their own site (standard ERPNext action, not something middleware initiates) -- so middleware has to go looking for it rather than already knowing its name.

	result = _request(
		franchise,
		"GET",
		"/api/resource/Sales Invoice",
		params={
			"filters": frappe.as_json(
				[["Sales Invoice Item", "sales_order", "=", sales_order_name], ["docstatus", "=", 1]]
			),
			"fields": frappe.as_json(["name"]),
			"limit_page_length": 1,
		},
	)
	rows = result.get("data") or []
	return rows[0]["name"] if rows else None


def cancel_document(franchise, doctype, name):
	_request(franchise, "PUT", f"/api/resource/{doctype}/{name}", payload={"docstatus": 2})


def create_credit_note(franchise, sales_invoice_name):
	# Reverses a paid invoice via a return Sales Invoice -- never cancels the original. Matches real accounting practice: a refund is a new document (credit note), not an
	# undo of a submitted invoice. make_sales_return handles the GL reversal and any restocking itself.
	credit_note = _get_mapped_doc(
		franchise,
		"erpnext.accounts.doctype.sales_invoice.sales_invoice.make_sales_return",
		sales_invoice_name,
	)
	result = _request(franchise, "POST", "/api/resource/Sales Invoice", payload=credit_note)
	name = result["data"]["name"]
	_request(franchise, "PUT", f"/api/resource/Sales Invoice/{name}", payload={"docstatus": 1})
	return name
