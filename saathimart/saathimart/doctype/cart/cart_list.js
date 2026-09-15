frappe.listview_settings["Cart"] = {
	add_fields: ["status", "grand_total"],
	get_indicator(doc) {
		if (doc.status === "CheckedOut") {
			return [__("Checked Out"), "green", "status,=,CheckedOut"];
		} else if (doc.status === "Abandoned") {
			return [__("Abandoned"), "orange", "status,=,Abandoned"];
		} else if (doc.status === "Expired") {
			return [__("Expired"), "gray", "status,=,Expired"];
		}
		return [__("Active"), "blue", "status,=,Active"];
	},
};
