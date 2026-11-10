const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "../public/js/cost_visibility.js"), "utf8");

function environment(restricted = true, initialViews = {}) {
	const callbacks = [];
	const selectors = new Map();
	let observer;
	let capture;
	const modelHandlers = {};
	function element() {
		const classes = new Set();
		return {
			classList: { add: (value) => classes.add(value), remove: (value) => classes.delete(value), contains: (value) => classes.has(value) },
			children: [], attributes: {}, isConnected: true, textContent: "",
			setAttribute(key, value) { this.attributes[key] = String(value); },
			getAttribute(key) { return this.attributes[key]; },
			appendChild(child) { this.children.push(child); },
		};
	}
	const document = {
		head: element(), body: element(),
		createElement: element,
		querySelectorAll: (selector) => selectors.get(selector) || [],
		addEventListener: (name, fn, useCapture) => { if (name === "click" && useCapture) capture = fn; },
	};
	const frappe = { boot: { cost_visibility: { restricted, patterns: ["purchase_rate", "stock value", "landed cost", "valuation"] } }, views: initialViews,
		model: { on: (doctype, fieldname, fn) => { modelHandlers[`${doctype}:${fieldname}`] = fn; } } };
	const window = { frappe, __: (s) => s, requestAnimationFrame: (fn) => callbacks.push(fn) };
	class MutationObserver {
		constructor(fn) { this.callback = fn; observer = this; }
		observe(target, options) { this.options = options; }
	}
	vm.runInNewContext(source, { window, document, frappe, MutationObserver });
	return { frappe, window, document, selectors, callbacks, element, modelHandlers, observer: () => observer, capture: () => capture };
}

{
	class QueryReport {
		get_menu_items() { return [{ label: "Add Column" }, { label: "Refresh" }]; }
	}
	const env = environment(true, { QueryReport });
	assert.deepEqual(Array.from(new env.frappe.views.QueryReport().get_menu_items(), (item) => item.label), ["Refresh"]);
}

{
	const env = environment(false);
	assert.equal(env.document.head.children.length, 0);
	assert.equal(env.observer(), undefined);
	assert.equal(Object.getOwnPropertyDescriptor(env.frappe.views, "ListView"), undefined);
}

{
	const env = environment();
	class ListView {
		get view_name() { return "List"; }
		setup_columns() {
			this.columns = [
				{ type: "Subject", df: { fieldname: "purchase_rate", label: "Purchase Rate" } },
				{ type: "Tag" },
				{ type: "Field", df: { fieldname: "selling_rate", label: "Selling Rate" } },
				{ type: "Field", df: { fieldname: "internal_value", label: "Stock Value" } },
			];
			return "list result";
		}
	}
	env.frappe.views.ListView = ListView;
	const list = new ListView();
	assert.equal(list.setup_columns(), "list result");
	assert.deepEqual(Array.from(list.columns, (c) => c.df?.fieldname || c.type), ["name", "Tag", "selling_rate"]);

	class ReportView extends ListView {
		get view_name() { return "Report"; }
		report_menu_items() { return [{ label: "Pick Columns" }, { label: "Print" }]; }
		add_column_to_datatable() { throw new Error("should be blocked"); }
		setup_datatable() { return "table result"; }
		render() { return "render result"; }
	}
	env.frappe.views.ReportView = ReportView;
	const report = new ReportView();
	const columns = [
		{ id: "_checkbox", colIndex: 0 },
		{ id: "name", colIndex: 1 },
		{ id: "purchase_rate", colIndex: 2, docfield: { label: "Purchase Rate" } },
		{ id: "selling_rate", colIndex: 3 },
	];
	const rows = [["A", 17, 99]];
	const node = env.element();
	const addNode = env.element();
	report.$datatable_wrapper = [node];
	report.datatable = {
		options: { headerDropdown: [{ label: "Sort" }, { label: "Add Column", action() {} }] },
		columnmanager: { $dropdownList: { children: [env.element(), addNode] } },
		datamanager: { getColumns: () => columns },
		rows,
	};
	assert.equal(report.setup_datatable(), "table result");
	assert.equal(addNode.classList.contains("cost-visibility-hidden"), true);
	assert.doesNotThrow(() => report.add_column_to_datatable());
	assert.doesNotThrow(() => report.datatable.options.headerDropdown[1].action());
	assert.deepEqual(Array.from(report.report_menu_items(), (item) => item.label), ["Print"]);
	assert.deepEqual(rows, [["A", 17, 99]]);
	assert.match(node.children[0].textContent, /data-cost-visibility-report="1".*dt-cell--col-2/);
	assert.doesNotMatch(node.children[0].textContent, /dt-cell--col-3/);
	columns[2].colIndex = 4;
	env.selectors.set(".cost-visibility-report", [node]);
	env.callbacks.shift()();
	assert.match(node.children[0].textContent, /dt-cell--col-4/);
	assert.equal(env.observer().options.attributes, undefined);

	class QueryReport {
		get_menu_items() { return [{ label: "Add Column" }, { label: "Export" }]; }
		render_datatable() { return "query result"; }
	}
	env.frappe.views.QueryReport = QueryReport;
	const query = new QueryReport();
	const queryNode = env.element();
	query.$report = [queryNode];
	query.datatable = { datamanager: { getColumns: () => [
		{ colIndex: 1, fieldname: "value", label: "Landed_Cost" },
		{ colIndex: 2, fieldname: "rate", label: "Rate" },
	] } };
	assert.equal(query.render_datatable(), "query result");
	assert.deepEqual(Array.from(query.get_menu_items(), (item) => item.label), ["Export"]);
	assert.match(queryNode.children[0].textContent, /data-cost-visibility-report="2".*dt-cell--col-1/);
	assert.doesNotMatch(queryNode.children[0].textContent, /dt-cell--col-2/);

	const field = env.element();
	field.fieldobj = { df: { fieldname: "value", label: "Stock Value" } };
	const safe = env.element();
	safe.fieldobj = { df: { fieldname: "rate", label: "Selling Rate" } };
	const search = env.element();
	const grid = env.element();
	grid.fieldobj = { grid: { header_search: { search_columns: { value: [search] } }, fields_map: { value: field.fieldobj.df } } };
	env.selectors.set(".std-form-layout .frappe-control[data-fieldname], .std-form-layout .grid-static-col[data-fieldname]", [field, safe]);
	env.selectors.set(".std-form-layout .frappe-control[data-fieldtype='Table'], .std-form-layout .frappe-control[data-fieldtype='Table MultiSelect']", [grid]);
	env.observer().callback();
	env.observer().callback();
	assert.equal(env.callbacks.length, 1, "observer work is coalesced");
	env.callbacks.shift()();
	assert.equal(field.classList.contains("cost-visibility-hidden"), true);
	assert.equal(search.classList.contains("cost-visibility-hidden"), true);
	assert.equal(safe.classList.contains("cost-visibility-hidden"), false);
	assert.equal(field.fieldobj.df.hidden, undefined, "shared DocField is not changed");

	const modal = { querySelector: () => ({}) };
	const link = { closest: (selector) => selector === ".modal" ? modal : null };
	const event = { target: { closest: (selector) => selector === ".add-new-fields" ? link : null },
		preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; } };
	env.capture()(event);
	assert.equal(event.prevented, true);
	assert.equal(event.stopped, true);
	const unrelated = { target: { closest: () => null }, preventDefault() { throw new Error("unrelated click blocked"); } };
	assert.doesNotThrow(() => env.capture()(unrelated));
}

{
	const env = environment();
	class ListView {
		get view_name() { return "List"; }
		setup_columns() {
			this.columns = ["name", "rate", "net_rate", "base_rate", "price_list_rate",
				"stock_uom_rate", "base_net_amount", "qty", "selling_rate"].map((name, i) => ({
				type: i ? "Field" : "Subject", df: { fieldname: name, label: name },
			}));
		}
	}
	env.frappe.views.ListView = ListView;
	for (const doctype of ["Purchase Invoice", "Purchase Order", "Purchase Receipt", "Supplier Quotation"]) {
		const list = new ListView();
		list.doctype = doctype;
		list.setup_columns();
		assert.deepEqual(Array.from(list.columns, (c) => c.df.fieldname), ["name", "qty", "selling_rate"], doctype);
	}
	for (const doctype of ["Sales Order", "Sales Invoice", "Item Price"]) {
		const list = new ListView();
		list.doctype = doctype;
		list.setup_columns();
		assert.equal(list.columns.length, 9, doctype);
	}
}

{
	const env = environment();
	const controls = ".std-form-layout .frappe-control[data-fieldname], .std-form-layout .grid-static-col[data-fieldname]";
	const tables = ".std-form-layout .frappe-control[data-fieldtype='Table'], .std-form-layout .frappe-control[data-fieldtype='Table MultiSelect']";
	const purchase = { doctype: "Purchase Order", doc: {} };
	const sales = { doctype: "Sales Order", doc: {} };
	const itemPrice = { doctype: "Item Price", doc: { buying: 1 } };
	function control(df, frm) {
		const node = env.element();
		node.fieldobj = { df, frm };
		return node;
	}
	const rateDf = { fieldname: "rate", label: "Rate", parent: "Purchase Order Item" };
	const itemRate = control(rateDf, purchase);
	const quantity = control({ fieldname: "qty", label: "Quantity", parent: "Purchase Order Item" }, purchase);
	const selling = control({ fieldname: "selling_rate", label: "Rate" }, purchase);
	const salesRate = control({ fieldname: "rate", label: "Rate", parent: "Sales Order Item" }, sales);
	const buyingPrice = control({ fieldname: "price_list_rate", label: "Price List Rate" }, itemPrice);
	const baseAmount = control({ fieldname: "base_net_amount", label: "Net Amount (Company Currency)" }, purchase);
	const gridCell = env.element();
	gridCell.df = { fieldname: "base_rate", label: "Rate (Company Currency)" };
	gridCell.closest = () => ({ fieldobj: { grid: { frm: purchase, doctype: "Purchase Order Item" } } });
	env.window.jQuery = (node) => ({ data: () => node.df });
	const gridSearch = env.element();
	const grid = env.element();
	grid.fieldobj = { grid: { frm: purchase, doctype: "Purchase Order Item", fields_map: { net_rate: { fieldname: "net_rate" } },
		header_search: { search_columns: { net_rate: [gridSearch] } } } };
	env.selectors.set(controls, [itemRate, quantity, selling, salesRate, buyingPrice, baseAmount, gridCell]);
	env.selectors.set(tables, [grid]);
	env.callbacks.shift()();
	for (const node of [itemRate, buyingPrice, baseAmount, gridCell, gridSearch]) {
		assert.equal(node.classList.contains("cost-visibility-hidden"), true);
	}
	for (const node of [quantity, selling, salesRate]) {
		assert.equal(node.classList.contains("cost-visibility-hidden"), false);
	}
	assert.equal(rateDf.hidden, undefined);
	itemPrice.doc.buying = 0;
	env.modelHandlers["Item Price:buying"]();
	env.callbacks.shift()();
	assert.equal(buyingPrice.classList.contains("cost-visibility-hidden"), false, "selling Item Price is restored");
	itemPrice.doc.buying = "1";
	env.modelHandlers["Item Price:buying"]();
	env.callbacks.shift()();
	assert.equal(buyingPrice.classList.contains("cost-visibility-hidden"), true);
}

{
	const env = environment();
	class QueryReport { render_datatable() {} }
	env.frappe.views.QueryReport = QueryReport;
	function render(reportName, refDoctype) {
		const report = new QueryReport();
		report.report_name = reportName;
		report.report_doc = { ref_doctype: refDoctype };
		const element = env.element();
		const columns = [
			{ colIndex: 1, fieldname: "rate", label: "Rate" },
			{ colIndex: 2, fieldname: "base_net_amount", label: "Net Amount (Company Currency)" },
			{ colIndex: 3, fieldname: "qty", label: "Quantity" },
			{ colIndex: 4, fieldname: "selling_rate", label: "Rate" },
			{ colIndex: 5, fieldname: "rate", label: "Rate", docfield: { parent: "Sales Order Item" } },
			{ colIndex: 6, fieldname: "price_list_rate", label: "Price List Rate" },
		];
		const rows = [[12, 20, 3, 40, 50, 60]];
		report.$report = [element];
		report.datatable = { datamanager: { getColumns: () => columns }, rows };
		report.render_datatable();
		assert.deepEqual(rows, [[12, 20, 3, 40, 50, 60]]);
		assert.equal(columns.length, 6);
		return element.children[0].textContent;
	}
	for (const [name, ref] of [["Item Wise Purchase Register", "Item"], ["Custom Report", "Purchase Receipt"]]) {
		const rules = render(name, ref);
		for (const index of [1, 2, 6]) assert.match(rules, new RegExp(`dt-cell--col-${index}\\b`));
		for (const index of [3, 4, 5]) assert.doesNotMatch(rules, new RegExp(`dt-cell--col-${index}\\b`));
	}
	assert.equal(render("Item Prices", "Item Price"), "", "mixed Item Price reports remain visible");
	assert.equal(render("Sales and Purchase Comparison", "Item"), "", "ambiguous mixed reports are not assumed to be purchases");
	assert.match(render("Buying Price Report", "Item Price"), /dt-cell--col-6/);
}

{
	const env = environment();
	class ReportView { render() {} }
	env.frappe.views.ReportView = ReportView;
	const report = new ReportView();
	report.doctype = "Purchase Invoice";
	report.fields = [["rate", "Purchase Invoice Item"], ["qty", "Purchase Invoice Item"]];
	const rows = [[42, 2]];
	const wrapper = env.element();
	report.$datatable_wrapper = [wrapper];
	report.datatable = { datamanager: { getColumns: () => [
		{ colIndex: 2, field: "rate", docfield: { fieldname: "rate", parent: "Purchase Invoice Item" } },
		{ colIndex: 3, field: "qty", docfield: { fieldname: "qty", parent: "Purchase Invoice Item" } },
	] }, rows };
	report.render();
	assert.match(wrapper.children[0].textContent, /dt-cell--col-2/);
	assert.doesNotMatch(wrapper.children[0].textContent, /dt-cell--col-3/);
	assert.deepEqual(report.fields, [["rate", "Purchase Invoice Item"], ["qty", "Purchase Invoice Item"]]);
	assert.deepEqual(rows, [[42, 2]]);
}

console.log("cost visibility client tests passed");
