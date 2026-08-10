frappe.ui.form.on("Payment Log", {
	refresh(frm) {
		const colours = { Success: "green", Failed: "red", Pending: "orange" };
		frm.page.set_indicator(frm.doc.status, colours[frm.doc.status] || "grey");

		if (frm.doc.order && !frm.is_new()) {
			frm.add_custom_button(__("View Order"), () => {
				frappe.set_route("Form", "Order", frm.doc.order);
			});
		}

		// Poll eSewa status for pending eSewa logs
		if (frm.doc.status === "Pending" && frm.doc.gateway === "eSewa" && frm.doc.order) {
			frm.add_custom_button(__("Check eSewa Status"), () => {
				frappe.call({
					method: "saathimart.api.payments.verify_esewa_status",
					args: { order_id: frm.doc.order },
					callback(r) {
						if (r.message) {
							frappe.msgprint(`eSewa status: <b>${r.message.status}</b>`);
							frm.reload_doc();
						}
					},
				});
			}, __("Actions"));
		}
	},
});
