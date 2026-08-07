import frappe


def get_authenticated_franchise():
	api_key = frappe.get_request_header("X-Saathi-Api-Key")
	api_secret = frappe.get_request_header("X-Saathi-Api-Secret")
	if not api_key or not api_secret:
		frappe.throw("Missing X-Saathi-Api-Key / X-Saathi-Api-Secret headers", frappe.AuthenticationError)

	site_code = frappe.db.get_value("Franchise", {"api_key": api_key}, "site_code")
	if not site_code:
		frappe.throw("Invalid API Key", frappe.AuthenticationError)

	franchise = frappe.get_doc("Franchise", site_code)
	if franchise.get_password("api_secret") != api_secret:
		frappe.throw("Invalid API Secret", frappe.AuthenticationError)

	if franchise.status != "Active":
		frappe.throw(f"Franchise {site_code} is {franchise.status}", frappe.PermissionError)

	return franchise
