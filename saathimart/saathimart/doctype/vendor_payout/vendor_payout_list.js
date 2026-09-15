frappe.listview_settings["Vendor Payout"] = {
	add_fields: ["status", "vendor", "payout_amount", "period_start", "period_end"],
	get_indicator(doc) {
		if (doc.status === "Paid") {
			return [__("Paid"), "green", "status,=,Paid"];
		} else if (doc.status === "Approved" || doc.status === "Confirmed") {
			return [__(doc.status), "blue", "status,=," + doc.status];
		} else if (doc.status === "Draft") {
			return [__("Draft"), "orange", "status,=,Draft"];
		} else if (doc.status === "Cancelled") {
			return [__("Cancelled"), "gray", "status,=,Cancelled"];
		}
	},
};
