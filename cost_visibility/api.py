import frappe


DEFAULT_PATTERNS = (
    "valuation rate",
    "valuation_rate",
    "purchase rate",
    "purchase_rate",
    "purchase price",
    "purchase_price",
    "buying price",
    "buying_price",
    "last purchase",
    "last_purchase",
    "landed cost",
    "landed_cost",
    "cost price",
    "cost_price",
    "stock value",
    "stock_value",
    "valuation",
)


@frappe.whitelist()
def get_policy():
    user = frappe.session.user
    if user in ("Administrator", "Guest"):
        return {"restricted": False, "patterns": []}

    settings = frappe.get_doc("Cost Visibility Settings")
    if not settings.enabled or not any(row.user == user for row in settings.restricted_users):
        return {"restricted": False, "patterns": []}

    patterns = list(DEFAULT_PATTERNS)
    for row in settings.extra_patterns:
        pattern = (row.pattern or "").strip().lower()
        if pattern and pattern not in patterns:
            patterns.append(pattern)
    return {"restricted": True, "patterns": patterns}


def extend_bootinfo(bootinfo):
    bootinfo.cost_visibility = get_policy()
