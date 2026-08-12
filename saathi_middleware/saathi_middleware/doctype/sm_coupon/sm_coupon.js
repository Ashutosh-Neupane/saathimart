frappe.ui.form.on("SM Coupon", {
	refresh(frm) {
		frm.page.set_indicator(
			frm.doc.is_active ? __("Active") : __("Inactive"),
			frm.doc.is_active ? "green" : "red"
		);

		if (!frm.is_new()) {
			const used = frm.doc.used_count || 0;
			const max  = frm.doc.max_uses || 0;
			frm.dashboard.add_comment(
				max > 0
					? __(`Used ${used} / ${max} times`)
					: __(`Used ${used} times (unlimited)`),
				used >= max && max > 0 ? "red" : "blue",
				true
			);
		}
	},

	coupon_type(frm) {
		frm.toggle_reqd("discount_percentage", frm.doc.coupon_type === "Percentage");
		frm.toggle_reqd("discount_amount",     frm.doc.coupon_type === "Fixed Amount");
	},
});
