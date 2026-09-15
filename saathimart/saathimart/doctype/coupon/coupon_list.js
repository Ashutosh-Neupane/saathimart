frappe.listview_settings["Coupon"] = {
	add_fields: ["is_active", "valid_from", "valid_to", "coupon_code"],
	get_indicator(doc) {
		if (!doc.is_active) {
			return [__("Disabled"), "gray", "is_active,=,0"];
		} else if (doc.valid_from && frappe.datetime.get_diff(doc.valid_from) > 0) {
			return [__("Scheduled"), "blue", "is_active,=,1|valid_from,>,Today"];
		} else if (doc.valid_to && frappe.datetime.get_diff(doc.valid_to) < 0) {
			return [__("Expired"), "red", "is_active,=,1|valid_to,<,Today"];
		}
		return [__("Active"), "green", "is_active,=,1"];
	},
};
