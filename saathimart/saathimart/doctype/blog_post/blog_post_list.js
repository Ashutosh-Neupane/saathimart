frappe.listview_settings["Blog Post"] = {
	add_fields: ["status", "published_at", "title"],
	get_indicator(doc) {
		if (doc.status === "Published") {
			return [__("Published"), "green", "status,=,Published"];
		} else if (doc.status === "Archived") {
			return [__("Archived"), "gray", "status,=,Archived"];
		}
		return [__("Draft"), "orange", "status,=,Draft"];
	},
};
