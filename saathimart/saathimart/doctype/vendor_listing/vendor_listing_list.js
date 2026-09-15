frappe.listview_settings["Vendor Listing"] = {
	add_fields: ["status", "sync_status", "vendor", "price"],
	get_indicator(doc) {
		if (doc.status === "Out of Stock") {
			return [__("Out of Stock"), "orange", "status,=,Out of Stock"];
		} else if (doc.status === "Active") {
			if (doc.sync_status === "Failed") {
				return [__("Active (sync failed)"), "red", "status,=,Active|sync_status,=,Failed"];
			}
			return [__("Active"), "green", "status,=,Active"];
		}
		return [__("Inactive"), "gray", "status,=,Inactive"];
	},
};
