/* UI preference only: this does not secure data, APIs, exports, or custom views. */
(() => {
	const policy = window.frappe?.boot?.cost_visibility;
	if (!policy?.restricted) return;

	const normalize = (value) => typeof value === "string"
		? value.toLowerCase().replace(/[_\s]+/g, " ").trim()
		: "";
	const patterns = (Array.isArray(policy.patterns) ? policy.patterns : [])
		.map(normalize).filter(Boolean);
	const purchase_doctype = (value) => /^(purchase (invoice|order|receipt)|supplier quotation)( item)?$/.test(normalize(value));
	const selling_doctype = (value) => /^(sales (invoice|order)|quotation|delivery note)( item)?$/.test(normalize(value));
	const purchase_fields = new Set([
		"rate", "net rate", "base rate", "base net rate", "price list rate",
		"base price list rate", "stock uom rate", "amount", "net amount", "base amount",
		"base net amount", "total", "net total", "base total", "base net total",
		"grand total", "base grand total", "rounded total", "base rounded total",
	]);
	const matches = (field, context = {}) => {
		if (!field) return false;
		if (typeof field === "string") field = { fieldname: field, label: field };
		const values = [field.fieldname, field.field, field.id, field.label, field.name, field.content,
			field.df?.fieldname, field.df?.label, field.docfield?.fieldname, field.docfield?.label];
		if (values.some((value) => {
				const text = normalize(value);
				return text && patterns.some((pattern) => text.includes(pattern));
			})) return true;

		// Exact generic names only; quantity, selling_rate, and arbitrary amount fields are not prices.
		if (values.some((value) => /\b(sales|selling)\b/.test(normalize(value)))) return false;
		const names = values.map((value) => normalize(typeof value === "string" ? value.split(":").pop() : value));
		const generic = names.some((name) => purchase_fields.has(name) ||
			/^(rate|net rate|price list rate|amount|net amount) \(company currency\)$/.test(name));
		if (!generic) return false;

		const frm = context.frm;
		if (normalize(frm?.doctype) === "item price") {
			return names.includes("price list rate") && (frm.doc?.buying === true || Number(frm.doc?.buying) === 1);
		}
		const owners = [field.parent, field.doctype, field.df?.parent, field.df?.doctype,
			field.docfield?.parent, field.docfield?.doctype, context.field_doctype];
		if (owners.some(selling_doctype)) return false;
		if (owners.some(purchase_doctype)) return true;
		const report_name = normalize(context.report?.report_name);
		const purchase_report = /\b(purchase|buying)\b|\bsupplier quotation\b/.test(report_name) &&
			!/\b(sales|selling)\b/.test(report_name);
		return purchase_doctype(context.doctype) || purchase_doctype(frm?.doctype) ||
			purchase_doctype(context.report?.doctype) || purchase_doctype(context.report?.report_doc?.ref_doctype) ||
			purchase_report;
	};
	const translated = (text) => typeof window.__ === "function" ? window.__(text) : text;
	const wrap = (prototype, name, make) => {
		if (!prototype || typeof prototype[name] !== "function") return;
		const original = prototype[name];
		if (original._cost_visibility) return;
		const replacement = make(original);
		replacement._cost_visibility = true;
		prototype[name] = replacement;
	};

	// The list and report bundles can be loaded after this app script.
	const views = frappe.views || (frappe.views = {});
	function watch_class(name, patch) {
		const descriptor = Object.getOwnPropertyDescriptor(views, name);
		if (descriptor && !descriptor.configurable) {
			patch(views[name]);
			return;
		}
		let value = views[name];
		patch(value);
		Object.defineProperty(views, name, {
			configurable: true,
			enumerable: true,
			get: () => value,
			set: (next) => { value = next; patch(next); },
		});
	}

	watch_class("ListView", (klass) => {
		wrap(klass?.prototype, "setup_columns", (original) => function (...args) {
			const result = original.apply(this, args);
			if (this.view_name !== "List" || !Array.isArray(this.columns)) return result;
			// ListView requires a subject at index zero, even when its title is sensitive.
			const context = { doctype: this.doctype };
			if (matches(this.columns[0]?.df, context)) {
				this.columns[0] = { type: "Subject", df: { fieldname: "name", label: translated("ID") } };
			}
			this.columns = this.columns.filter((column) => !matches(column.df, context));
			return result;
		});
	});

	let report_id = 0;
	function report_style(report, wrapper) {
		const element = wrapper?.[0];
		if (!element || !report.datatable) return;
		if (!element._cost_visibility_id) {
			element._cost_visibility_id = ++report_id;
			element.setAttribute("data-cost-visibility-report", element._cost_visibility_id);
		}
		element._cost_visibility_report = report;
		element.classList.add("cost-visibility-report");
		update_report_style(element);
	}

	function update_report_style(element) {
		const table = element._cost_visibility_report?.datatable;
		const columns = table?.datamanager?.getColumns?.();
		if (!Array.isArray(columns)) return;
		let style = element._cost_visibility_style;
		if (!style || !style.isConnected) {
			style = document.createElement("style");
			element.appendChild(style);
			element._cost_visibility_style = style;
		}
		// Style elements are global even inside a wrapper, so scope every rule explicitly.
		const report = element._cost_visibility_report;
		const rules = columns.filter((column) => matches(column, { report })).map((column) => column.colIndex)
			.filter((index) => Number.isInteger(index) && index >= 0)
			.map((index) => `[data-cost-visibility-report="${element._cost_visibility_id}"] .dt-cell--col-${index}{display:none!important}`).join("\n");
		if (style.textContent !== rules) style.textContent = rules;
	}

	watch_class("ReportView", (klass) => {
		wrap(klass?.prototype, "report_menu_items", (original) => function (...args) {
			return original.apply(this, args).filter((item) => item.label !== translated("Pick Columns"));
		});
		wrap(klass?.prototype, "add_column_to_datatable", () => function () {});
		wrap(klass?.prototype, "setup_datatable", (original) => function (...args) {
			const result = original.apply(this, args);
			const table = this.datatable;
			const items = table?.options?.headerDropdown || [];
			items.forEach((item, index) => {
				if (item.label !== translated("Add Column")) return;
				item.action = () => {};
				const node = table.columnmanager?.$dropdownList?.children[index];
				if (node) node.classList.add("cost-visibility-hidden");
			});
			report_style(this, this.$datatable_wrapper);
			return result;
		});
		wrap(klass?.prototype, "render", (original) => function (...args) {
			const result = original.apply(this, args);
			report_style(this, this.$datatable_wrapper);
			return result;
		});
	});

	watch_class("QueryReport", (klass) => {
		wrap(klass?.prototype, "get_menu_items", (original) => function (...args) {
			return original.apply(this, args).filter((item) => item.label !== translated("Add Column"));
		});
		wrap(klass?.prototype, "render_datatable", (original) => function (...args) {
			const result = original.apply(this, args);
			report_style(this, this.$report);
			return result;
		});
	});

	const style = document.createElement("style");
	style.textContent = `
		.cost-visibility-hidden { display: none !important; }
		.std-form-layout .grid-heading-row .grid-static-col.pointer { display: none !important; }
	`;
	document.head.appendChild(style);

	function hide(node) {
		if (node && !node.classList.contains("cost-visibility-hidden")) {
			node.classList.add("cost-visibility-hidden");
		}
	}
	function set_field_hidden(node, hidden) {
		if (!node) return;
		if (hidden) hide(node);
		else if (node.classList.contains("cost-visibility-hidden")) node.classList.remove("cost-visibility-hidden");
	}

	function field_context(node, df) {
		const control = node.fieldobj;
		const grid = node.closest?.(".frappe-control[data-fieldtype='Table'], .frappe-control[data-fieldtype='Table MultiSelect']")?.fieldobj?.grid;
		let frm = control?.frm || grid?.frm;
		const current = window.cur_frm;
		if (!frm && current?.body?.[0]?.contains(node)) frm = current;
		return { frm, field_doctype: control?.doctype || grid?.doctype || df?.parent };
	}

	function is_list_settings(modal) {
		return modal?.querySelector("[data-fieldname='disable_count']") &&
			modal.querySelector("[data-fieldname='disable_auto_refresh']");
	}
	// Capture also covers a click before the next observer scan.
	document.addEventListener("click", (event) => {
		const target = event.target;
		if (!target?.closest) return;
		const add = target.closest(".add-new-fields");
		const grid_settings = target.closest(".std-form-layout .grid-heading-row .grid-static-col.pointer");
		if (grid_settings || (add && is_list_settings(add.closest(".modal")))) {
			event.preventDefault();
			event.stopImmediatePropagation();
		}
	}, true);

	function scan() {
		// fieldobj and grid-cell data refer to the actual control's DocField, not a global name map.
		document.querySelectorAll(".std-form-layout .frappe-control[data-fieldname], .std-form-layout .grid-static-col[data-fieldname]")
			.forEach((node) => {
				const df = node.fieldobj?.df || (window.jQuery && window.jQuery(node).data("df"));
				const field = df || { fieldname: node.getAttribute("data-fieldname") };
				set_field_hidden(node, matches(field, field_context(node, df)));
			});
		document.querySelectorAll(".std-form-layout .frappe-control[data-fieldtype='Table'], .std-form-layout .frappe-control[data-fieldtype='Table MultiSelect']")
			.forEach((node) => {
				const grid = node.fieldobj?.grid;
				if (!grid?.header_search?.search_columns) return;
				Object.entries(grid.header_search.search_columns).forEach(([name, column]) => {
					const df = grid.fields_map?.[name] || { fieldname: name };
					set_field_hidden(column?.[0], matches(df, { frm: grid.frm, field_doctype: grid.doctype }));
				});
			});
		// ListSettings is a module-private class; identify its dialog by its own controls.
		document.querySelectorAll(".modal").forEach((modal) => {
			if (is_list_settings(modal)) {
				modal.querySelectorAll("[data-fieldname='fields_html'] .add-new-fields").forEach(hide);
			}
		});
		document.querySelectorAll(".cost-visibility-report").forEach(update_report_style);
	}

	let scheduled = false;
	function schedule() {
		if (scheduled) return;
		scheduled = true;
		window.requestAnimationFrame(() => {
			scheduled = false;
			scan();
		});
	}
	// Observe structural changes only. Our class changes do not re-trigger the observer.
	const observer = new MutationObserver(schedule);
	observer.observe(document.body, { childList: true, subtree: true });
	if (window.jQuery) window.jQuery(document).on("form-refresh page-change grid-row-render", schedule);
	if (frappe.router?.on) frappe.router.on("change", schedule);
	// Item Price's buying flag is often fetched programmatically from the Price List.
	if (frappe.model?.on) frappe.model.on("Item Price", "buying", schedule);
	schedule();
})();
