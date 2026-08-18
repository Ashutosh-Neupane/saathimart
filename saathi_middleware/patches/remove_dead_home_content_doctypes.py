"""
SM Hero Slide / SM Seasonal Banner / SM Trust Badge / SM Product Rail
Heading / SM Homepage Settings / SM Website Content backed
api.cms.get_home_content()/get_content() — confirmed the frontend never
calls either (it uses get_banners/SM Banner for hero+promo content
instead; see seed_static_page_content.py, which seeds that real path).
Same situation as remove_sm_coupon: deleting the .json/.py files alone
doesn't drop the DocType record or its table, so do that explicitly here.
"""
import frappe

_DEAD_DOCTYPES = (
	"SM Hero Slide",
	"SM Seasonal Banner",
	"SM Trust Badge",
	"SM Product Rail Heading",
	"SM Homepage Settings",
	"SM Website Content",
)


def execute():
	for doctype in _DEAD_DOCTYPES:
		if frappe.db.exists("DocType", doctype):
			frappe.delete_doc("DocType", doctype, ignore_permissions=True, force=True)
		frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `tab{doctype}`")

	frappe.db.commit()
