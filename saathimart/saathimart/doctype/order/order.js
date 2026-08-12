frappe.ui.form.on("Order", {
	refresh(frm) {
		const colours = {
			Pending:            "orange",
			Confirmed:          "blue",
			Preparing:          "purple",
			"Out for Delivery": "cyan",
			Delivered:          "green",
			Cancelled:          "red",
			Refunded:           "grey",
		};
		frm.page.set_indicator(frm.doc.status, colours[frm.doc.status] || "grey");

		if (!frm.is_new()) {
			// Status transition buttons
			const transitions = {
				Pending:            ["Confirmed", "Cancelled"],
				Confirmed:          ["Preparing", "Cancelled"],
				Preparing:          ["Out for Delivery"],
				"Out for Delivery": ["Delivered"],
			};
			(transitions[frm.doc.status] || []).forEach(s => {
				frm.add_custom_button(__(s), () => {
					frappe.call({
						method: "saathimart.api.orders.update_order_status",
						args: { order_id: frm.doc.name, status: s },
						callback(r) { if (!r.exc) frm.reload_doc(); },
					});
				}, __("Update Status"));
			});

			// Initiate payment for unpaid online orders
			if (frm.doc.payment_status === "Unpaid" &&
				["eSewa"].includes(frm.doc.payment_method)) {
				frm.add_custom_button(__("Initiate Payment"), () => {
					frappe.call({
						method: "saathimart.api.payments.initiate_payment",
						args: { method: frm.doc.payment_method.toLowerCase(), order_id: frm.doc.name },
						callback(r) { if (!r.exc) frm.reload_doc(); },
					});
				});
			}

			// Loyalty points info
			if (frm.doc.loyalty_points_earned) {
				frm.dashboard.add_comment(
					__(`🎁 ${frm.doc.loyalty_points_earned} loyalty points earned`), "green", true
				);
			}
			if (frm.doc.loyalty_points_redeemed) {
				frm.dashboard.add_comment(
					__(`✅ ${frm.doc.loyalty_points_redeemed} points redeemed (NPR ${frm.doc.loyalty_discount} off)`),
					"blue", true
				);
			}
		}
	},

	// Recalculate totals live in the form (mirrors calculate_taxes_and_totals)
	delivery_charge(frm) { _recalc(frm); },
	discount_amount(frm)  { _recalc(frm); },
	coupon_code(frm)      { _recalc(frm); },
	loyalty_points_redeemed(frm) { _recalc(frm); },
});

frappe.ui.form.on("Order Item", {
	qty(frm)          { _recalc(frm); },
	rate(frm)         { _recalc(frm); },
	items_remove(frm) { _recalc(frm); },
});

function _recalc(frm) {
	const items = frm.doc.items || [];
	let net_total = 0;
	items.forEach(row => {
		row.amount = flt(row.qty) * flt(row.rate);
		net_total += row.amount;
	});
	frm.set_value("subtotal", net_total);

	// Simple client-side preview — server does the authoritative calc on save
	const delivery   = flt(frm.doc.delivery_charge);
	const manual_dis = flt(frm.doc.discount_amount);
	const loyalty_dis= flt(frm.doc.loyalty_discount);
	const coupon_dis = flt(frm.doc.coupon_discount);
	const grand = Math.max(net_total + delivery - manual_dis - loyalty_dis - coupon_dis, 0);
	frm.set_value("grand_total", grand);
	frm.refresh_field("items");
}
