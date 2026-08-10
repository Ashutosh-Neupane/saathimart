frappe.ui.form.on("Product", {
	refresh(frm) {
		frm.add_custom_button(__("View on Store"), () => {
			if (frm.doc.slug) {
				window.open(`/products/${frm.doc.slug}`, "_blank");
			}
		});

		if (frm.doc.stock_qty <= frm.doc.low_stock_threshold && frm.doc.track_inventory) {
			frm.dashboard.add_comment(__("⚠️ Low stock"), "orange", true);
		}

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

	price(frm) {
		if (frm.doc.compare_price && frm.doc.compare_price < frm.doc.price) {
			frappe.msgprint(__("Compare price should be higher than selling price."));
		}
	},
});
