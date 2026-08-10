frappe.ui.form.on("Cart", {
	refresh(frm) {
		const colours = {
			Active:      "green",
			CheckedOut:  "blue",
			Abandoned:   "orange",
			Expired:     "grey",
		};
		frm.page.set_indicator(frm.doc.status, colours[frm.doc.status] || "grey");

		if (frm.doc.status === "Active" && !frm.is_new()) {
			frm.add_custom_button(__("Convert to Order"), () => {
				frappe.prompt([
					{ fieldname: "customer_name",  fieldtype: "Data",        label: "Customer Name",  reqd: 1 },
					{ fieldname: "customer_phone", fieldtype: "Data",        label: "Phone",          reqd: 1 },
					{ fieldname: "delivery_address", fieldtype: "Small Text", label: "Delivery Address", reqd: 1 },
					{ fieldname: "payment_method", fieldtype: "Select",      label: "Payment Method",
					  options: "COD\neSewa\nKhalti\nBank Transfer", default: "COD" },
				], (values) => {
					frappe.call({
						method: "saathimart.api.orders.checkout",
						args: {
							session_id: frm.doc.session_id,
							...values,
						},
						callback(r) {
							if (r.message && r.message.order_id) {
								frappe.set_route("Form", "Order", r.message.order_id);
							}
						},
					});
				}, __("Convert Cart to Order"), __("Create Order"));
			});
		}
	},
});

frappe.ui.form.on("Cart Item", {
	qty(frm)  { _recalc(frm); },
	rate(frm) { _recalc(frm); },
	items_remove(frm) { _recalc(frm); },
});

function _recalc(frm) {
	let subtotal = 0;
	(frm.doc.items || []).forEach(row => {
		subtotal += (row.qty || 0) * (row.rate || 0);
	});
	frm.set_value("subtotal", subtotal);
}
