frappe.listview_settings["Vendor"] = {
	add_fields: ["status", "hub_status", "vendor_name"],
	get_indicator(doc) {
		if (doc.status === "Active" && doc.hub_status === "Active") {
			return [__("Active"), "green", "status,=,Active"];
		} else if (doc.status === "Active" && doc.hub_status === "Unreachable") {
			return [__("Unreachable"), "orange", "hub_status,=,Unreachable"];
		} else if (doc.status === "Pending") {
			return [__("Pending"), "yellow", "status,=,Pending"];
		} else if (doc.status === "Suspended") {
			return [__("Suspended"), "red", "status,=,Suspended"];
		}
	},
};
