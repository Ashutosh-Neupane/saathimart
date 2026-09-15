frappe.listview_settings["Review"] = {
	add_fields: ["status", "rating", "product"],
	get_indicator(doc) {
		if (doc.status === "Approved") {
			return [__("Approved"), "green", "status,=,Approved"];
		} else if (doc.status === "Rejected") {
			return [__("Rejected"), "red", "status,=,Rejected"];
		}
		return [__("Pending"), "orange", "status,=,Pending"];
	},
};
