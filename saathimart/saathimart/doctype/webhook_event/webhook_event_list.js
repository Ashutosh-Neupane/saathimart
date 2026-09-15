frappe.listview_settings["Webhook Event"] = {
	add_fields: ["status", "event_type", "dead_letter_reason"],
	get_indicator(doc) {
		if (doc.status === "Dead") {
			return [__("Dead"), "red", "status,=,Dead"];
		} else if (doc.status === "Failed") {
			return [__("Failed"), "red", "status,=,Failed"];
		} else if (doc.status === "Queued") {
			return [__("Queued"), "orange", "status,=,Queued"];
		} else if (doc.status === "Sent") {
			return [__("Sent"), "green", "status,=,Sent"];
		} else if (doc.status === "Skipped") {
			return [__("Skipped"), "gray", "status,=,Skipped"];
		}
	},
};
