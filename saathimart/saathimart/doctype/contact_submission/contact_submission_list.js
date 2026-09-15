frappe.listview_settings["Contact Submission"] = {
	add_fields: ["status", "email"],
	get_indicator(doc) {
		if (doc.status === "Replied") {
			return [__("Replied"), "green", "status,=,Replied"];
		} else if (doc.status === "Read") {
			return [__("Read"), "blue", "status,=,Read"];
		} else if (doc.status === "Spam") {
			return [__("Spam"), "gray", "status,=,Spam"];
		}
		return [__("New"), "orange", "status,=,New"];
	},
};
