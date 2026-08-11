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


def create_sales_order(franchise, customer, address_name, items, delivery_date):
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


def create_paid_sales_invoice(franchise, sales_order_name, mode_of_payment, paid_amount):
	invoice = _get_mapped_doc(
		franchise, "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice", sales_order_name
	)
	invoice["is_pos"] = 1
	invoice["update_stock"] = 1
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
