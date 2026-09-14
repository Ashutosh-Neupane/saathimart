// Stream Health dashboard — consumes saathimart.streams.api (StreamMonitor,
// DLQ) endpoints. Auto-refreshes every 30s; manual refresh + DLQ controls.

frappe.pages['stream-health'].on_page_load = function(wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('Stream Health'),
		single_column: true
	});

	page.set_primary_action(__('Refresh'), () => load(page), 'refresh');

	$('<div class="sm-stream-health">'
		+ '<p class="text-muted" id="sm-sh-updated"></p>'
		+ '<div id="sm-sh-body" style="margin-top:12px"></div>'
		+ '</div>').appendTo(page.main);

	load(page);
	// 30s auto-refresh keeps the view honest without hammering Redis
	page.sm_interval = setInterval(() => load(page), 30000);

	// DLQ row action (event delegation — rows re-render every refresh)
	$(document).on('click', '.sm-sh-retry', function() {
		const vendor = this.dataset.vendor, msgId = this.dataset.msg;
		frappe.call({
			method: 'saathimart.streams.api.retry_dead_letter',
			args: { vendor_id: vendor, msg_id: msgId },
			callback: () => { frappe.show_alert({message: __('Retried'), indicator: 'green'}); load(page); }
		});
	});
	$(document).on('click', '.sm-sh-ack', function() {
		const vendor = this.dataset.vendor, msgId = this.dataset.msg;
		frappe.call({
			method: 'saathimart.streams.api.acknowledge_dead_letter',
			args: { vendor_id: vendor, msg_id: msgId },
			callback: () => { frappe.show_alert({message: __('Acknowledged'), indicator: 'green'}); load(page); }
		});
	});
};

frappe.pages['stream-health'].on_page_show = function(wrapper) {
	// resume auto-refresh only while the page is visible
	const page = frappe.pages['stream-health'].page;
	if (!page.sm_interval) page.sm_interval = setInterval(() => load(page), 30000);
};

function load(page) {
	frappe.call({
		method: 'saathimart.streams.api.get_full_health_snapshot',
		callback: (r) => {
			if (!r.message) return;
			render(page, r.message);
		},
		error: () => {
			$('#sm-sh-body').html('<div class="alert alert-danger">Failed to reach Redis Streams — check the cache Redis.</div>');
		}
	});
}

function esc(s) {
	return frappe.utils.escape_html(String(s == null ? '' : s));
}

function render(page, snap) {
	$('#sm-sh-updated').text(__('Updated {0} · auto-refresh 30s',
		[frappe.datetime.str_to_user(snap.checked_at)]));

	let html = '';

	// ── Mirror switch ──
	html += '<div class="panel panel-default"><div class="panel-heading"><strong>'
		+ __('Second Transport (Redis Streams)') + '</strong></div><div class="panel-body">'
		+ '<span class="indicator ' + (snap.mirror_enabled ? 'green' : 'orange') + '">'
		+ (snap.mirror_enabled ? __('Mirror ENABLED') : __('Mirror DISABLED'))
		+ '</span> — ' + esc(snap.mirror_note || '') + '</div></div>';

	// ── Per-vendor streams ──
	if (!snap.vendors.length) {
		html += '<div class="alert alert-default">' + __('No vendor streams found yet.') + '</div>';
	}
	snap.vendors.forEach((v) => {
		const state = v.length === 0 && v.pending === 0 ? 'green'
			: (v.length > 5000 || v.dead_letters > 0 ? 'red' : 'orange');
		html += '<div class="panel panel-default"><div class="panel-heading">'
			+ '<strong>' + esc(v.vendor) + '</strong>'
			+ '<span class="indicator ' + state + '" style="margin-left:8px">'
			+ (state === 'green' ? __('healthy') : (state === 'red' ? __('attention') : __('backlog')))
			+ '</span></div><div class="panel-body"><table class="table table-condensed" style="margin-bottom:6px">'
			+ '<tr><td>' + __('Stream length (undelivered+history)') + '</td><td><b>' + v.length + '</b></td></tr>'
			+ '<tr><td>' + __('Pending (read, unacked)') + '</td><td><b>' + v.pending + '</b></td></tr>'
			+ '<tr><td>' + __('Dead letters') + '</td><td><b>' + v.dead_letters + '</b></td></tr>'
			+ '</table>';
		if (v.consumers && v.consumers.length) {
			html += '<p class="text-muted small">' + __('Consumers') + ': '
				+ v.consumers.map((c) => esc(c.consumer) + ' (' + c.count + ')').join(', ') + '</p>';
		}
		html += '</div></div>';
	});

	// ── Dead letters table ──
	if (snap.dead_letters.length) {
		html += '<div class="panel panel-danger"><div class="panel-heading"><strong>'
			+ __('Dead Letter Queue') + '</strong></div><div class="panel-body">'
			+ '<table class="table table-striped"><thead><tr>'
			+ '<th>' + __('Vendor') + '</th><th>' + __('Event') + '</th><th>' + __('ID') + '</th><th></th></tr></thead><tbody>';
		snap.dead_letters.forEach((d) => {
			html += '<tr><td>' + esc(d.vendor) + '</td><td>' + esc(d.event_type) + '</td><td class="small">'
				+ esc(d.msg_id) + '</td><td class="text-right">'
				+ '<button class="btn btn-xs btn-default sm-sh-retry" data-vendor="' + esc(d.vendor)
				+ '" data-msg="' + esc(d.msg_id) + '">' + __('Retry') + '</button> '
				+ '<button class="btn btn-xs btn-default sm-sh-ack" data-vendor="' + esc(d.vendor)
				+ '" data-msg="' + esc(d.msg_id) + '">' + __('Discard') + '</button></td></tr>';
		});
		html += '</tbody></table></div></div>';
	}

	$('#sm-sh-body').html(html);
}
