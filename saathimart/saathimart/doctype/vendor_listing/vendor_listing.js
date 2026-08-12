frappe.ui.form.on("Vendor Listing", {
	price(frm) {
		if (frm.doc.compare_price && frm.doc.compare_price < frm.doc.price) {
			frappe.msgprint(__("Compare price should be higher than selling price."));
		}
	},

	compare_price(frm) {
		if (frm.doc.compare_price && frm.doc.compare_price < frm.doc.price) {
			frappe.msgprint(__("Compare price should be higher than selling price."));
		}
	},
});
