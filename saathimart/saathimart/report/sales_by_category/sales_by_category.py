"""Sales by Category — which categories actually generate GMV.

Joins Order Item to Product for the category, aggregates revenue, units,
margin proxy (discount share), and coupon exposure per category.
"""
import frappe
from frappe.utils import add_days, flt, today


def execute(filters=None):
    filters = filters or {}
    from_date = filters.get("from_date") or add_days(today(), -30)
    to_date = filters.get("to_date") or today()

    data = frappe.db.sql(
        """
        SELECT
            COALESCE(cat.category_name, 'Uncategorised') AS category,
            COUNT(DISTINCT o.name) AS orders,
            SUM(oi.qty) AS units_sold,
            SUM(oi.amount) AS gross_revenue,
            SUM(o.coupon_discount + o.loyalty_discount + o.onboarding_discount) * SUM(oi.amount) / NULLIF(SUM(o.grand_total), 0) AS discount_share
        FROM `tabOrder Item` oi
        JOIN `tabOrder` o ON o.name = oi.parent
        JOIN `tabProduct` p ON p.name = oi.product
        LEFT JOIN `tabCategory` cat ON cat.name = p.category
        WHERE o.docstatus < 2
          AND DATE(o.creation) BETWEEN %s AND %s
        GROUP BY cat.category_name
        ORDER BY gross_revenue DESC
        """,
        (from_date, to_date),
        as_dict=True,
    )

    columns = [
        {"label": "Category", "fieldname": "category", "fieldtype": "Data", "width": 220},
        {"label": "Orders", "fieldname": "orders", "fieldtype": "Int", "width": 90},
        {"label": "Units Sold", "fieldname": "units_sold", "fieldtype": "Int", "width": 110},
        {"label": "Gross Revenue", "fieldname": "gross_revenue", "fieldtype": "Currency", "width": 150},
        {"label": "Discount Share", "fieldname": "discount_share", "fieldtype": "Percent", "width": 130},
    ]
    return columns, data
