frappe.ui.form.on("Product", {
	refresh(frm) {
		frm.add_custom_button(__("View on Store"), () => {
			if (frm.doc.slug) {
				window.open(`https://www.saathimart.com/products/${frm.doc.slug}`, "_blank");
			}
		});

		// A per-product low-stock banner doesn't make sense in a
		// multi-vendor model anyway — "low stock" is a Vendor Listing
		// concept (see Vendor Stock Report), not a single number on
		// Product. This used to check frm.doc.low_stock_threshold, a
		// field that was removed from Product entirely by the v1_to_v2
		// migration (see saathimart/migrations/v1_to_v2.py) — the check
		// silently never fired again after that, since the field just
		// read back undefined.

		if (frm.doc.status === "Active") {
			frm.add_custom_button(__("Mark Inactive"), () => {
				frm.set_value("status", "Inactive");
				frm.save();
			}, __("Actions"));
		} else if (frm.doc.status !== "Active") {
			frm.add_custom_button(__("Mark Active"), () => {
				frm.set_value("status", "Active");
				frm.save();
			}, __("Actions"));
		}
	},

	product_name(frm) {
		if (!frm.doc.slug) {
			frm.set_value("slug", frm.doc.product_name.toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, ""));
		}
	},

	// price/compare_price used to have a compare-price validation handler
	// here, but they're Python-only properties (saathimart.saathimart.doctype.
	// product.product.Product.price/.compare_price), derived from Vendor
	// Listing — never real, editable form fields on Product, so this
	// handler could never actually fire. Moved to vendor_listing.js, where
	// price/compare_price are real fields someone actually types into.
});
