frappe.ui.form.on("Delivery Zone", {
	refresh(frm) {
		frm.page.set_indicator(
			frm.doc.is_active ? __("Active") : __("Inactive"),
			frm.doc.is_active ? "green" : "grey"
		);

		if (!frm.is_new()) {
			frm.add_custom_button(__("View Orders"), () => {
				frappe.set_route("List", "Order", { delivery_zone: frm.doc.name });
			});
		}
	},

	free_delivery_above(frm) {
		if (frm.doc.free_delivery_above > 0) {
			frm.set_intro(
				__(`Free delivery on orders above NPR ${frm.doc.free_delivery_above}`),
				"green"
			);
		}
	},
});
