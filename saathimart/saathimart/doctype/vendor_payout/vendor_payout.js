frappe.ui.form.on("Vendor Payout", {
	refresh(frm) {
		if (frm.doc.vendor && !frm.is_new()) {
			frm.add_custom_button(__("View Vendor"), () => {
				frappe.set_route("Form", "Vendor", frm.doc.vendor);
			});
		}
	},
});
