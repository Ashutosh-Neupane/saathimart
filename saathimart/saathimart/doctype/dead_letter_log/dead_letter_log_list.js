frappe.listview_settings["Dead Letter Log"] = {
	add_fields: ["status", "event_type"],
	get_indicator(doc) {
		if (doc.status === "Resolved") {
			return [__("Resolved"), "green", "status,=,Resolved"];
		} else if (doc.status === "Retried") {
			return [__("Retried"), "blue", "status,=,Retried"];
		} else if (doc.status === "Ignored") {
			return [__("Ignored"), "gray", "status,=,Ignored"];
		}
		return [__("Pending"), "red", "status,=,Pending"];
	},
};
