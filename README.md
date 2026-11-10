# Cost Visibility

Cost Visibility filters recognised cost fields for selected users in supported
Desk views, report exports and document print bodies. It does not protect all
cost data: APIs, custom print formats and other paths can expose values. It does
not change existing permissions. Do not use it as a security control.

## Setup

1. As a System Manager, open **Cost Visibility Settings** in Desk.
2. Enable the setting and add users in **Restricted Users**.
3. Optionally add literal field names or labels in **Additional Cost Phrases**.
4. Save and ask affected users to reload Desk.

The setting has no effect until users are added. Administrator cannot be selected.
Other users retain their existing document and report permissions.

Standard form fields, child grids, list views and report columns are matched by
field name or label. In purchase documents and clearly named purchase reports,
generic rate and amount fields are also hidden. On an Item Price form, the price
list rate is hidden when the document is marked buying. Standard list, grid and
report column-adding controls are disabled for selected users.

For selected users, Query/Script Report responses and their CSV and Excel exports
are filtered on the server. Their standard Desk print/PDF uses the filtered
report data. Report Builder list data and CSV/Excel exports are filtered too.
Query Report charts and summaries are suppressed for selected users because they
may contain costs. Standard Frappe document print bodies receive a redacted copy
of parent and child fields without changing the saved document.

## Limitations

- Cost classification is heuristic. Custom aliases, formulas, totals and mixed
  buying/selling reports may reveal costs. Review actual reports and add specific
  phrases; broad phrases can also hide unrelated values.
- Document letterheads and footers render before the print-body hook. Custom
  Jinja can query the database or use another object. Builder-beta/WeasyPrint,
  raw printing, document attachments and other PDF paths are not guaranteed to
  be filtered. Templates that use controller methods may fail when filtered.
- Prepared Report raw attachments, scheduled reports, files generated before a
  policy change, other APIs and direct Python calls are not covered. Report
  Builder filtering may blank ambiguous non-cost columns.
- These filters are not field-level permissions and do not secure the underlying
  values. Administrator and Guest are not restricted.

## Client Policy

The policy is available synchronously as `frappe.boot.cost_visibility` when Desk
loads. After settings change, affected users must reload Desk to receive the new
boot policy. The existing `cost_visibility.api.get_policy` endpoint can also be
called as the current session user. Both provide only `restricted` (boolean) and
`patterns` (list of lowercase strings). Patterns are literal, case-insensitive
substrings, not regular expressions. They include cost-specific defaults and any configured extra
phrases; generic `rate` and `price` are intentionally excluded from defaults.
For unrestricted users, `patterns` is empty. Treat matching as a presentation
preference, not authorization. Broad custom phrases can affect unrelated fields.
