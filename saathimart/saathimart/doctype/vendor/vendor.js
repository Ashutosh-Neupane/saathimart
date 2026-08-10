frappe.ui.form.on("Vendor", {
	refresh(frm) {
		const colours = { Active: "green", Pending: "orange", Suspended: "red" };
		frm.page.set_indicator(frm.doc.status, colours[frm.doc.status] || "grey");

		if (!frm.is_new()) {
			frm.add_custom_button(__("View Orders"), () => {
				frappe.set_route("List", "Order", { vendor: frm.doc.name });
			});

			frm.add_custom_button(__("View Products"), () => {
				frappe.set_route("List", "Product", { vendor: frm.doc.name });
			});

			if (frm.doc.status === "Pending") {
				frm.add_custom_button(__("Approve Vendor"), () => {
					frm.set_value("status", "Active");
					frm.save();
				}, __("Actions"));
			}
			if (frm.doc.status === "Active") {
				frm.add_custom_button(__("Suspend Vendor"), () => {
					frm.set_value("status", "Suspended");
					frm.save();
				}, __("Actions"));
			}
		}
	},

	vendor_name(frm) {
		if (!frm.doc.slug) {
			frm.set_value("slug", frm.doc.vendor_name.toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, ""));
		}
	},
});
