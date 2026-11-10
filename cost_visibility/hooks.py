app_name = "cost_visibility"
app_title = "Cost Visibility"
app_publisher = "Cost Visibility"
app_description = "Presentation-only cost visibility preferences"
app_email = "cost_visibility@example.com"
app_license = "MIT"

app_include_js = "/assets/cost_visibility/js/cost_visibility.js"
extend_bootinfo = "cost_visibility.api.extend_bootinfo"

override_whitelisted_methods = {
    "frappe.desk.query_report.run": "cost_visibility.report_filter.run",
    "frappe.desk.query_report.export_query": "cost_visibility.report_filter.export_query",
    "frappe.desk.reportview.get": "cost_visibility.report_builder.get",
    "frappe.desk.reportview.export_query": "cost_visibility.report_builder.export_query",
}

pdf_body_html = "cost_visibility.print_filter.pdf_body_html"
