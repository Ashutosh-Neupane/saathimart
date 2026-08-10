frappe.ui.form.on("Review", {
	refresh(frm) {
		const colours = { Approved: "green", Pending: "orange", Rejected: "red" };
		frm.page.set_indicator(frm.doc.status, colours[frm.doc.status] || "grey");

		if (frm.doc.status === "Pending" && !frm.is_new()) {
			frm.add_custom_button(__("Approve"), () => {
				frm.set_value("status", "Approved");
				frm.save();
			}, __("Actions"));

			frm.add_custom_button(__("Reject"), () => {
				frm.set_value("status", "Rejected");
				frm.save();
			}, __("Actions"));
		}

		if (frm.doc.product) {
			frm.add_custom_button(__("View Product"), () => {
				frappe.set_route("Form", "Product", frm.doc.product);
			});
		}
	},
});
