// SaathiMart Ops Dashboard — one-glance health of the marketplace.
// Data comes from saathimart.ops_dashboard.get_snapshot(); the page is a
// thin renderer so numbers stay server-computed (Desk users can trust them).
frappe.pages["ops-dashboard"].on_page_load = function (wrapper) {
	frappe.ui.make_app_page({
		parent: wrapper,
		title: "Ops Dashboard",
		single_column: true,
	});

	wrapper.ops = new OpsDashboard(wrapper);
};

class OpsDashboard {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.page = wrapper.page;
		this.render_shell();
		this.bind_actions();
		this.refresh();
	}

	render_shell() {
		this.$container = $(`<div class="sm-ops" style="padding:15px;">
			<div class="sm-ops-stamp" style="color:var(--text-muted);font-size:12px;margin-bottom:10px;"></div>
			<div class="sm-ops-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;"></div>
			<div class="sm-ops-alerts" style="margin-top:18px;"></div>
		</div>`).appendTo(this.page.main);
	}

	bind_actions() {
		this.page.set_primary_action(__("Refresh"), () => this.refresh());
	}

	refresh() {
		frappe
			.call("saathimart.saathimart.page.ops_dashboard.ops_dashboard.get_snapshot")
			.then((r) => r.message && this.render_snapshot(r.message));
	}

	render_snapshot(s) {
		this.$container.find(".sm-ops-stamp").text(
			__("Snapshot as of {0}", [s.generated_at])
		);

		const sections = [
			{ title: __("Today"), cards: s.today },
			{ title: __("Pipeline Health"), cards: s.pipeline },
			{ title: __("Money"), cards: s.money },
		];

		const $grid = this.$container.find(".sm-ops-grid").empty();
		sections.forEach((sec) => {
			$grid.append(
				$(`<div style="grid-column:1/-1;font-weight:600;margin-top:6px;">${sec.title}</div>`)
			);
			sec.cards.forEach((c) => {
				const warn = c.warn ? "border-left:3px solid var(--warning);" : "";
				$grid.append(
					`<div class="sm-ops-card" style="background:var(--card-bg);border-radius:8px;padding:12px;${warn}">
						<div style="font-size:12px;color:var(--text-muted);">${c.label}</div>
						<div style="font-size:22px;font-weight:700;">${c.value}</div>
						${c.sub ? `<div style="font-size:11px;color:var(--text-muted);">${c.sub}</div>` : ""}
					</div>`
				);
			});
		});

		const $alerts = this.$container.find(".sm-ops-alerts").empty();
		if (s.alerts && s.alerts.length) {
			s.alerts.forEach((a) =>
				$alerts.append(`<div class="alert alert-${a.level}" style="margin-bottom:6px;">${a.text}</div>`)
			);
		} else {
			$alerts.append(`<div class="text-muted">${__("No alerts.")}</div>`);
		}
	}
}
