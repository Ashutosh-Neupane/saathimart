frappe.ui.form.on("Loyalty Point Entry", {
	refresh(frm) {
		const colours = {
			Earned:   "green",
			Redeemed: "orange",
			Expired:  "grey",
			Adjusted: "blue",
		};
		frm.page.set_indicator(frm.doc.entry_type, colours[frm.doc.entry_type] || "grey");

		if (frm.doc.order) {
			frm.add_custom_button(__("View Order"), () => {
				frappe.set_route("Form", "Order", frm.doc.order);
			});
		}
	},
});
