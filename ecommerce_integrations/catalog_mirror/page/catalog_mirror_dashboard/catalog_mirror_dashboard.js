// Catalog Mirror Dashboard — operator-facing single-page overview of
// every Catalog Mirror with inline Adopt picker, per-row Preview / Sync
// buttons, and bulk "Preview all" / "Sync all adopted" actions.
//
// Style mirrors ecommerce_integrations/public/js/catalog_mirror/setting_widget.js:
// - plain HTML strings built via template literals
// - all user-facing strings via __()
// - all attribute / cell content escaped via frappe.utils.escape_html
// - indicator-pill vocabulary identical to setting_widget.js
//
// No React / Vue / Webpack — jQuery + frappe.ui only.

frappe.provide("ecommerce_integrations.catalog_mirror");

frappe.pages["catalog-mirror-dashboard"].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: __("Catalog Mirror Dashboard"),
        single_column: true,
    });
    new ecommerce_integrations.catalog_mirror.Dashboard(wrapper, page);
};


ecommerce_integrations.catalog_mirror.Dashboard = class {
    constructor(wrapper, page) {
        this.wrapper = $(wrapper).find(".layout-main-section");
        this.page = page;
        this.backend_filter = "";  // empty = all backends
        this.rows = [];
        this._expanded = {};  // mirror name -> bool
        this._poll_timer = null;
        this._render_shell();
        this._render_toolbar_buttons();
        this.reload();
    }

    // ── Shell / toolbar ──────────────────────────────────────────────

    _render_shell() {
        this.wrapper.html(`
            <div class="cmd-root" style="padding:0">
                <div class="cmd-intro text-muted small" style="margin-bottom:0.5em">
                    ${__("All Catalog Mirrors at a glance. Pick suggestions to link unmapped mirrors, then run Sync.")}
                </div>
                <div class="cmd-body">
                    <div class="text-muted small">${__("Loading…")}</div>
                </div>
            </div>
        `);
    }

    _render_toolbar_buttons() {
        const me = this;

        // Backend filter (top-right)
        this.backend_select = this.page.add_field({
            fieldname: "backend",
            label: __("Backend"),
            fieldtype: "Select",
            options: [
                { value: "", label: __("All backends") },
                { value: "Shopware", label: "Shopware" },
                { value: "Medusa", label: "Medusa" },
            ],
            change() {
                me.backend_filter = this.get_value() || "";
                me.reload();
            },
        });

        this.page.set_primary_action(__("Sync all adopted"), () => me.sync_all_adopted(), "play");
        this.page.add_menu_item(__("Preview all"), () => me.preview_all());
        this.page.add_menu_item(__("Refresh"), () => me.reload());
    }

    // ── Data load ────────────────────────────────────────────────────

    reload() {
        const me = this;
        const $body = this.wrapper.find(".cmd-body");
        $body.html(`<div class="text-muted small">${__("Loading…")}</div>`);

        frappe.call({
            method: "ecommerce_integrations.catalog_mirror.api_dashboard.list_with_suggestions",
            args: { backend: this.backend_filter || null },
            callback(r) {
                me.rows = (r && r.message) || [];
                me._render_table();
            },
            error() {
                $body.html(
                    `<div class="text-muted small">${__("Catalog Mirror module not available — run bench migrate.")}</div>`,
                );
            },
        });
    }

    // ── Renderers ────────────────────────────────────────────────────

    _render_table() {
        if (!this.rows.length) {
            this.wrapper.find(".cmd-body").html(
                `<div class="text-muted">${__("No Catalog Mirrors configured yet.")}</div>`,
            );
            return;
        }

        const head = `
            <thead><tr>
                <th style="width:30px"></th>
                <th>${__("Title")}</th>
                <th style="width:160px">${__("Root Item Group")}</th>
                <th style="width:160px">${__("Storefront")}</th>
                <th style="width:90px">${__("Backend")}</th>
                <th style="width:80px">${__("Linked")}</th>
                <th style="width:90px">${__("Status")}</th>
                <th style="width:160px">${__("Last Synced")}</th>
                <th style="width:240px">${__("Actions")}</th>
            </tr></thead>
        `;

        const body = this.rows.map((row) => this._render_row(row)).join("");

        this.wrapper.find(".cmd-body").html(
            `<table class="table table-sm cmd-table" style="margin-bottom:0">${head}<tbody>${body}</tbody></table>`,
        );
        this._wire_row_handlers();
    }

    _render_row(row) {
        const expanded = !!this._expanded[row.name];
        const status = _status_badge(row);
        const linked = _adopted_badge(row);
        const sales_channel = row.target_sales_channel || row.target_sales_channel_data || "—";
        const last_synced = row.last_synced_at
            ? frappe.datetime.str_to_user(row.last_synced_at)
            : "—";
        const mirror_url = `/app/ecommerce-catalog-mirror/${encodeURIComponent(row.name)}`;
        const title_html = `<strong>${frappe.utils.escape_html(row.title || row.name)}</strong>`
            + (row.is_active ? "" : ` <span class="text-muted small">(${__("inactive")})</span>`);

        const main = `
            <tr class="cmd-row" data-mirror="${frappe.utils.escape_html(row.name)}">
                <td><span class="cmd-toggle" style="cursor:pointer;display:inline-block;width:1em">${expanded ? "▾" : "▸"}</span></td>
                <td>${title_html}</td>
                <td class="small">${frappe.utils.escape_html(row.root_item_group || "—")}</td>
                <td class="small text-muted">${frappe.utils.escape_html(sales_channel)}</td>
                <td class="small">${frappe.utils.escape_html(row.backend || "—")}</td>
                <td>${linked}</td>
                <td>${status}</td>
                <td class="small text-muted">${last_synced}</td>
                <td>
                    <button class="btn btn-xs btn-default cmd-preview" data-mirror="${frappe.utils.escape_html(row.name)}">${__("Preview")}</button>
                    <button class="btn btn-xs btn-default cmd-sync" data-mirror="${frappe.utils.escape_html(row.name)}">${__("Sync now")}</button>
                    <a class="btn btn-xs btn-default" href="${mirror_url}">${__("Open")}</a>
                </td>
            </tr>
        `;

        const details = expanded ? this._render_details(row) : "";
        return main + details;
    }

    _render_details(row) {
        const lines = [];
        if (row.external_root_id) {
            lines.push(`<div class="small"><strong>${__("External Root ID")}:</strong> <code>${frappe.utils.escape_html(row.external_root_id)}</code></div>`);
        } else {
            lines.push(`<div class="small text-muted">${__("Not linked yet — pick a suggestion below or open the mirror form.")}</div>`);
        }
        if (row.last_synced_at) {
            lines.push(`<div class="small text-muted">${__("Last sync")}: ${frappe.datetime.comment_when(row.last_synced_at)}</div>`);
        }
        if (row.last_error) {
            const snippet = (row.last_error || "").split("\n")[0].slice(0, 240);
            lines.push(`<div class="small text-danger">${__("Last error")}: ${frappe.utils.escape_html(snippet)}</div>`);
        }

        const suggestions = !row.external_root_id ? this._render_suggestions(row) : "";

        return `
            <tr class="cmd-details" data-parent="${frappe.utils.escape_html(row.name)}">
                <td></td>
                <td colspan="8" style="background:var(--bg-light, transparent)">
                    ${lines.join("")}
                    ${suggestions}
                </td>
            </tr>
        `;
    }

    _render_suggestions(row) {
        const list = row.suggestions || [];
        if (!list.length) {
            return `<div class="small text-muted" style="margin-top:0.5em">${__("No automatic suggestions found for this root Item Group.")}</div>`;
        }
        const items = list.map((s) => {
            const path = s.path ? frappe.utils.escape_html(s.path) : frappe.utils.escape_html(s.name || "");
            const ext = frappe.utils.escape_html(s.external_id);
            const count = (s.linked_product_count != null) ? s.linked_product_count : "?";
            return `
                <li style="margin:0.25em 0">
                    <div>
                        <strong>${frappe.utils.escape_html(s.name || "")}</strong>
                        <span class="text-muted small"> — ${path} · ${__("{0} products", [count])}</span>
                        <div class="small text-muted"><code>${ext}</code></div>
                    </div>
                    <div style="margin-top:0.25em">
                        <button class="btn btn-xs btn-primary cmd-adopt"
                                data-mirror="${frappe.utils.escape_html(row.name)}"
                                data-ext="${ext}"
                                data-mode="link_only">
                            ${__("Link & add only")}
                        </button>
                        <button class="btn btn-xs btn-default cmd-adopt"
                                data-mirror="${frappe.utils.escape_html(row.name)}"
                                data-ext="${ext}"
                                data-mode="full_sync">
                            ${__("Link & full sync")}
                        </button>
                    </div>
                </li>
            `;
        }).join("");

        return `
            <div style="margin-top:0.5em">
                <div class="small"><strong>${__("Suggestions ({0})", [list.length])}:</strong></div>
                <ul style="list-style:none;padding-left:0;margin:0">${items}</ul>
            </div>
        `;
    }

    // ── Wiring ───────────────────────────────────────────────────────

    _wire_row_handlers() {
        const me = this;
        const $body = this.wrapper.find(".cmd-body");

        // Single delegated handler for the toggle column.
        $body.off("click.cmd").on("click.cmd", ".cmd-toggle", function () {
            const mirror = $(this).closest("tr").data("mirror");
            if (!mirror) return;
            me._expanded[mirror] = !me._expanded[mirror];
            me._render_table();
        });

        $body.on("click.cmd", ".cmd-preview", function (e) {
            e.stopPropagation();
            const mirror = $(this).data("mirror");
            me.preview_one(mirror);
        });

        $body.on("click.cmd", ".cmd-sync", function (e) {
            e.stopPropagation();
            const mirror = $(this).data("mirror");
            frappe.confirm(
                __("Sync this mirror now against the live backend?"),
                () => me.sync_one(mirror),
            );
        });

        $body.on("click.cmd", ".cmd-adopt", function (e) {
            e.stopPropagation();
            const mirror = $(this).data("mirror");
            const ext = $(this).data("ext");
            const mode = $(this).data("mode") || "link_only";
            me.adopt(mirror, String(ext), mode);
        });
    }

    // ── Actions ──────────────────────────────────────────────────────

    preview_one(mirror) {
        frappe.call({
            method: "ecommerce_integrations.catalog_mirror.api.preview_mirror",
            args: { mirror },
            freeze: true,
            freeze_message: __("Building preview…"),
            callback(r) {
                if (!r.message) return;
                _show_single_preview_dialog(mirror, r.message);
            },
        });
    }

    sync_one(mirror) {
        const me = this;
        frappe.call({
            method: "ecommerce_integrations.catalog_mirror.api.apply_mirror_now",
            args: { mirror },
            freeze: true,
            freeze_message: __("Syncing…"),
            callback(r) {
                if (!r.message) return;
                const res = r.message;
                frappe.show_alert({
                    message: __("Status: {0} — created: {1}, updated: {2}, moved: {3}, deleted: {4}", [
                        res.status, res.created || 0, res.updated || 0, res.moved || 0, res.deleted || 0,
                    ]),
                    indicator: res.status === "ok" ? "green" : "red",
                });
                me.reload();
            },
        });
    }

    adopt(mirror, external_id, mode) {
        const me = this;
        frappe.call({
            method: "ecommerce_integrations.catalog_mirror.api_dashboard.adopt_root",
            args: { mirror, external_id, mode },
            freeze: true,
            freeze_message: __("Linking…"),
            callback(r) {
                if (!r.message) return;
                frappe.show_alert({
                    message: __("Linked: {0}", [mirror]),
                    indicator: "green",
                });
                me.reload();
            },
        });
    }

    preview_all() {
        const me = this;
        frappe.call({
            method: "ecommerce_integrations.catalog_mirror.api_dashboard.preview_all",
            args: { backend: this.backend_filter || null },
            freeze: true,
            freeze_message: __("Building previews…"),
            callback(r) {
                if (!r.message) return;
                _show_aggregate_preview_dialog(r.message);
                me.reload();
            },
        });
    }

    sync_all_adopted() {
        const me = this;
        frappe.confirm(
            __("Run Sync against every linked (adopted) mirror? Un-linked mirrors are skipped."),
            () => frappe.call({
                method: "ecommerce_integrations.catalog_mirror.api_dashboard.sync_all_adopted",
                args: { backend: me.backend_filter || null },
                freeze: true,
                freeze_message: __("Enqueueing syncs…"),
                callback(r) {
                    if (!r.message) return;
                    const s = r.message;
                    frappe.show_alert({
                        message: __("Enqueued: {0} · Skipped (not linked): {1} · Skipped (inactive): {2}", [
                            s.enqueued || 0, s.skipped_unadopted || 0, s.skipped_inactive || 0,
                        ]),
                        indicator: "blue",
                    });
                    me._start_polling();
                },
            }),
        );
    }

    // ── Polling ──────────────────────────────────────────────────────

    _start_polling() {
        const me = this;
        if (this._poll_timer) clearTimeout(this._poll_timer);
        const started_at = Date.now();
        const tick = () => {
            // Poll for ~30 seconds at 2 s intervals.
            if (Date.now() - started_at > 30000) {
                me.reload();
                return;
            }
            frappe.call({
                method: "ecommerce_integrations.catalog_mirror.api_dashboard.list_with_suggestions",
                args: { backend: me.backend_filter || null },
                callback(r) {
                    me.rows = (r && r.message) || [];
                    me._render_table();
                    const still_running = me.rows.some(
                        (row) => row.sync_status === "running" || row.sync_status === "pending",
                    );
                    if (still_running) {
                        me._poll_timer = setTimeout(tick, 2000);
                    }
                },
                error() {
                    // Stop polling on transport errors — manual refresh
                    // is one click away.
                },
            });
        };
        this._poll_timer = setTimeout(tick, 2000);
    }
};


// ── Stateless helpers ────────────────────────────────────────────────

function _status_badge(row) {
    const map = { ok: "green", error: "red", running: "blue", pending: "orange" };
    const colour = map[row.sync_status] || "gray";
    const label = row.sync_status || "pending";
    return `<span class="indicator-pill ${colour}">${frappe.utils.escape_html(label)}</span>`;
}


function _adopted_badge(row) {
    if (row.external_root_id) {
        return `<span class="indicator-pill green" title="${frappe.utils.escape_html(row.external_root_id)}">${__("linked")}</span>`;
    }
    return `<span class="indicator-pill orange">${__("not linked")}</span>`;
}


function _show_single_preview_dialog(mirror, plan) {
    // Lightweight summary — the rich tree-diff dialog lives on the
    // single-mirror form. This dashboard preview is a quick "do I want
    // to open the form?" decision aid.
    const c = {
        create: (plan.creates || []).length,
        update: (plan.updates || []).length,
        move: (plan.moves || []).length,
        noop: (plan.noops || []).length,
        orphan: (plan.orphans || []).length,
    };

    const root_live = plan.external_root_id
        ? frappe.utils.escape_html(plan.external_root_id)
        : `<em class="text-muted">${__("not yet linked")}</em>`;

    const html = `
        <div style="margin-bottom:0.5em">
            <div><strong>${frappe.utils.escape_html(plan.root_item_group || mirror)}</strong>
                <span class="text-muted">→</span> ${root_live}</div>
        </div>
        <div>
            <span class="indicator-pill green">${__("Create")}: ${c.create}</span>
            <span class="indicator-pill orange">${__("Update")}: ${c.update}</span>
            <span class="indicator-pill yellow">${__("Move")}: ${c.move}</span>
            <span class="indicator-pill gray">${__("Noop")}: ${c.noop}</span>
            <span class="indicator-pill red">${__("Orphan")}: ${c.orphan}</span>
        </div>
        <div class="text-muted small" style="margin-top:0.5em">
            ${__("Open the mirror form for the full tree-diff and Adopt controls.")}
        </div>
    `;

    const d = new frappe.ui.Dialog({
        title: __("Preview: {0}", [plan.title || mirror]),
        fields: [{ fieldname: "preview_html", fieldtype: "HTML" }],
        primary_action_label: __("Open mirror"),
        primary_action() {
            d.hide();
            frappe.set_route("Form", "Ecommerce Catalog Mirror", mirror);
        },
    });
    d.fields_dict.preview_html.$wrapper.html(html);
    d.show();
}


function _show_aggregate_preview_dialog(summary) {
    const rows = (summary.per_mirror || []).map((m) => {
        const link = `/app/ecommerce-catalog-mirror/${encodeURIComponent(m.mirror)}`;
        return `<tr>
            <td><a href="${link}">${frappe.utils.escape_html(m.title || m.mirror)}</a></td>
            <td class="small">${frappe.utils.escape_html(m.backend || "")}</td>
            <td class="text-right">${m.create || 0}</td>
            <td class="text-right">${m.update || 0}</td>
            <td class="text-right">${m.move || 0}</td>
            <td class="text-right">${m.noop || 0}</td>
            <td class="text-right">${m.orphan || 0}</td>
            <td class="small text-danger">${m.error ? frappe.utils.escape_html(m.error) : ""}</td>
        </tr>`;
    }).join("");

    const totals = summary.totals || {};
    const top = `
        <div style="margin-bottom:0.5em">
            <span class="indicator-pill green">${__("Create")}: ${totals.create || 0}</span>
            <span class="indicator-pill orange">${__("Update")}: ${totals.update || 0}</span>
            <span class="indicator-pill yellow">${__("Move")}: ${totals.move || 0}</span>
            <span class="indicator-pill gray">${__("Noop")}: ${totals.noop || 0}</span>
            <span class="indicator-pill red">${__("Orphan")}: ${totals.orphan || 0}</span>
            <span class="indicator-pill red">${__("Errors")}: ${totals.error || 0}</span>
        </div>
    `;
    const table = `
        <table class="table table-sm">
            <thead><tr>
                <th>${__("Mirror")}</th><th>${__("Backend")}</th>
                <th class="text-right">${__("Create")}</th>
                <th class="text-right">${__("Update")}</th>
                <th class="text-right">${__("Move")}</th>
                <th class="text-right">${__("Noop")}</th>
                <th class="text-right">${__("Orphan")}</th>
                <th>${__("Error")}</th>
            </tr></thead>
            <tbody>${rows}</tbody>
        </table>
    `;

    const d = new frappe.ui.Dialog({
        title: __("Preview all — combined summary"),
        size: "large",
        fields: [{ fieldname: "html", fieldtype: "HTML" }],
    });
    d.fields_dict.html.$wrapper.html(top + table);
    d.show();
}
