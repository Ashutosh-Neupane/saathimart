frappe.ui.form.on("Loyalty Program", {
	refresh(frm) {
		frm.page.set_indicator(
			frm.doc.is_active ? __("Active") : __("Inactive"),
			frm.doc.is_active ? "green" : "grey"
		);

		if (!frm.is_new()) {
			frm.add_custom_button(__("View Point Entries"), () => {
				frappe.set_route("List", "Loyalty Point Entry", { program: frm.doc.name });
			});
		}
	},

	collection_factor(frm) {
		if (frm.doc.collection_factor > 0) {
			const pts = Math.round(1000 * frm.doc.collection_factor);
			frm.set_intro(__(`Customer earns ${pts} points per NPR 1,000 spent`), "blue");
		}
	},
});
