frappe.ui.form.on("Webhook Event", {
	refresh(frm) {
		const colours = { Sent: "green", Queued: "orange", Failed: "red", Skipped: "grey" };
		frm.page.set_indicator(frm.doc.status, colours[frm.doc.status] || "grey");

		if (frm.doc.status === "Failed" && !frm.is_new()) {
			frm.add_custom_button(__("Retry Now"), () => {
				frm.set_value("status", "Queued");
				frm.set_value("retry_count", 0);
				frm.save();
				frappe.show_alert({ message: __("Event re-queued for delivery"), indicator: "green" });
			}, __("Actions"));
		}
	},
});
