frappe.listview_settings["Order"] = {
	add_fields: ["status", "payment_status", "payment_method", "grand_total", "customer_name", "delivery_date"],
	get_indicator(doc) {
		if (doc.status === "Cancelled" || doc.status === "Refunded") {
			return [__(doc.status), "gray", "status,=," + doc.status];
		} else if (doc.status === "Delivered") {
			return [__("Delivered"), "green", "status,=,Delivered"];
		} else if (doc.status === "Out for Delivery") {
			return [__("Out for Delivery"), "orange", "status,=,Out for Delivery"];
		} else if (doc.status === "Preparing") {
			return [__("Preparing"), "orange", "status,=,Preparing"];
		} else if (doc.status === "Confirmed") {
			return [__("Confirmed"), "blue", "status,=,Confirmed"];
		} else if (doc.status === "Pending") {
			return [__("Pending"), "red", "status,=,Pending"];
		}
	},
};
