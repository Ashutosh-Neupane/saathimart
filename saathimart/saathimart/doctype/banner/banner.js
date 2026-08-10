frappe.ui.form.on("Banner", {
	refresh(frm) {
		frm.page.set_indicator(
			frm.doc.is_active ? __("Active") : __("Inactive"),
			frm.doc.is_active ? "green" : "grey"
		);

		if (frm.doc.image) {
			frm.set_intro(
				`<img src="${frm.doc.image}" style="max-height:120px;border-radius:8px;margin-top:8px;">`,
				"blue"
			);
		}
	},

	title(frm) {
		if (!frm.doc.heading) {
			frm.set_value("heading", frm.doc.title);
		}
	},
});
