frappe.ui.form.on("SM Cart", {
	refresh(frm) {
		const subtotal = frm.doc.subtotal || 0;
		frm.fields_dict.subtotal.$wrapper.html(
			`<span class="text-muted">Subtotal:</span> <b>${format_currency(subtotal)}</b>`
		);
	},
});
