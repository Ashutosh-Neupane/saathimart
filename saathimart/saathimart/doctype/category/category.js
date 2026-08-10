frappe.ui.form.on("Category", {
	refresh(frm) {
		frm.page.set_indicator(
			frm.doc.is_active ? __("Active") : __("Inactive"),
			frm.doc.is_active ? "green" : "grey"
		);

		if (!frm.is_new()) {
			frm.add_custom_button(__("View Products"), () => {
				frappe.set_route("List", "Product", { category: frm.doc.name });
			});
		}
	},

	category_name(frm) {
		if (!frm.doc.slug) {
			frm.set_value("slug", frm.doc.category_name.toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, ""));
		}
	},
});
