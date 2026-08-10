frappe.ui.form.on("Site Page", {
	refresh(frm) {
		const colours = { Published: "green", Draft: "orange", Archived: "grey" };
		frm.page.set_indicator(frm.doc.status, colours[frm.doc.status] || "grey");

		if (frm.doc.slug && frm.doc.status === "Published") {
			frm.add_custom_button(__("View Page"), () => {
				window.open(`/${frm.doc.slug}`, "_blank");
			});
		}

		if (frm.doc.status === "Draft" && !frm.is_new()) {
			frm.add_custom_button(__("Publish"), () => {
				frm.set_value("status", "Published");
				frm.save();
			}, __("Actions"));
		}
	},

	title(frm) {
		if (!frm.doc.slug) {
			frm.set_value("slug", frm.doc.title.toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, ""));
		}
	},
});
