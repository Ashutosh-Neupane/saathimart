"""
Membership — a paid plan that discounts specific categories.

Coupons and onboarding discounts are order-level: they take net_total and
return one number. A category rule cannot work that way — "20% off
Vegetables" has to know which lines are vegetables — so this module resolves
per cart line and sums. That is the only structural difference from the
discounts already in api/totals.py; everything else (Percentage/Fixed Amount,
min_order_amount, max_discount_amount) deliberately reuses Coupon's
vocabulary so the admin forms read the same.

Free delivery is NOT a benefit rule. Delivery charge is computed from the
Delivery Zone, outside the line-item path, so it lives on the plan as its own
flag and is applied in totals.py alongside the coupon's free_delivery.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt, getdate, nowdate


# ── Lookup ───────────────────────────────────────────────────────────────────

def get_active_membership(customer_email):
    """The one Active, unexpired membership for this customer, or None.

    Expiry is filtered in SQL rather than trusting `status`: the nightly
    expiry job may not have run yet, and a membership that lapsed at midnight
    must stop discounting immediately, not whenever the scheduler next fires.
    """
    if not customer_email:
        return None

    rows = frappe.get_all(
        "Customer Membership",
        filters={
            "customer_email": customer_email,
            "status": "Active",
            "expires_on": [">=", nowdate()],
        },
        fields=["name", "plan", "expires_on"],
        order_by="expires_on desc",
        limit=1,
    )
    return rows[0] if rows else None


def _category_ancestry(category):
    """A category plus every ancestor, nearest first.

    Category has a parent_category, so a rule on "Groceries" should cover
    "Vegetables" underneath it. Returning nearest-first lets the caller prefer
    the most specific rule that matches.
    """
    chain = []
    seen = set()
    current = category
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        current = frappe.db.get_value("Category", current, "parent_category")
    return chain


def _product_facts(product_names):
    """category + brand for each product, in one query rather than per line."""
    if not product_names:
        return {}
    rows = frappe.get_all(
        "Product",
        filters={"name": ["in", list(product_names)]},
        fields=["name", "category", "brand"],
    )
    return {r.name: r for r in rows}


# ── Rule matching ────────────────────────────────────────────────────────────

def _benefit_matches(benefit, facts):
    """Does this rule cover this product? Returns match specificity, or None.

    Specificity decides ties before priority does: an exact category match
    beats an inherited parent-category match, which beats an All Products
    catch-all. Without this, a broad "5% off everything" rule authored at a
    low priority would quietly outrank a deliberate "20% off Vegetables".
    """
    if not benefit.get("is_active"):
        return None

    scope = benefit.get("scope")
    if scope == "All Products":
        return 1000

    if scope == "Brand":
        brand = (facts.get("brand") or "").strip().lower()
        target = (benefit.get("brand") or "").strip().lower()
        return 0 if (brand and target and brand == target) else None

    if scope == "Category":
        chain = _category_ancestry(facts.get("category"))
        target = benefit.get("category")
        if target in chain:
            return chain.index(target)  # 0 = exact, 1 = parent, 2 = grandparent…
        return None

    return None


def _pick_benefit(benefits, facts, net_total):
    """The single rule that applies to one line: most specific, then lowest
    priority. One rule per line, never two — stacked category discounts
    produce totals no customer-support agent can explain."""
    candidates = []
    for b in benefits:
        if flt(b.get("min_order_amount")) > flt(net_total):
            continue
        specificity = _benefit_matches(b, facts)
        if specificity is None:
            continue
        candidates.append((specificity, int(b.get("priority") or 0), b))

    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


def _benefit_label(benefit):
    """Human text frozen into the ledger at time of use."""
    if benefit.get("discount_type") == "Percentage":
        amount = f"{flt(benefit.get('value')):g}%"
    else:
        amount = f"{flt(benefit.get('value')):g}"

    scope = benefit.get("scope")
    if scope == "Category":
        target = benefit.get("category")
    elif scope == "Brand":
        target = benefit.get("brand")
    else:
        target = "all products"
    return f"{amount} off {target}"


# ── Resolution ───────────────────────────────────────────────────────────────

def resolve_membership_discount(items, customer_email, net_total):
    """Pure function: given cart lines and a customer, return what membership
    takes off. No writes, so totals preview and the real Order share it.

    `items` are dicts/child rows with at least `product`, `amount`.
    Returns {discount, membership, plan, free_delivery, lines, breakdown}.
    """
    blank = {
        "discount": 0.0,
        "membership": None,
        "plan": None,
        "free_delivery": 0,
        "free_delivery_min_order": 0.0,
        "lines": [],
        "breakdown": [],
    }

    membership = get_active_membership(customer_email)
    if not membership:
        return blank

    plan = frappe.get_cached_doc("Membership Plan", membership["plan"])
    if not plan.is_active:
        return blank

    benefits = [b.as_dict() for b in (plan.benefits or [])]
    facts_by_product = _product_facts({i.get("product") for i in items if i.get("product")})

    per_rule_total = {}   # row name -> running total, for that rule's own cap
    line_results = []
    breakdown = {}

    for item in items:
        amount = flt(item.get("amount") or 0)
        if amount <= 0:
            continue

        facts = facts_by_product.get(item.get("product"))
        facts = facts.as_dict() if hasattr(facts, "as_dict") else (facts or {})

        benefit = _pick_benefit(benefits, facts, net_total)
        if not benefit:
            continue

        if benefit.get("discount_type") == "Percentage":
            saved = amount * flt(benefit.get("value")) / 100
        else:
            # Fixed amount is per matching line, floored at the line value so a
            # line can never go negative and hand back more than it cost.
            saved = min(flt(benefit.get("value")), amount)

        # Per-rule ceiling, tracked across every line the rule touched.
        cap = flt(benefit.get("max_discount_amount"))
        if cap > 0:
            used = per_rule_total.get(benefit.get("name"), 0.0)
            saved = max(min(saved, cap - used), 0.0)
        per_rule_total[benefit.get("name")] = per_rule_total.get(benefit.get("name"), 0.0) + saved

        if saved <= 0:
            continue

        label = _benefit_label(benefit)
        line_results.append({
            "product": item.get("product"),
            "amount": round(amount, 2),
            "benefit_label": label,
            "saved": round(saved, 2),
        })
        breakdown[label] = round(breakdown.get(label, 0.0) + saved, 2)

    total = round(sum(l["saved"] for l in line_results), 2)

    # Plan-wide ceiling, applied last. Scale the line results down
    # proportionally so the ledger rows still sum to the charged discount.
    plan_cap = flt(plan.max_discount_per_order)
    if plan_cap > 0 and total > plan_cap:
        factor = plan_cap / total if total else 0
        for l in line_results:
            l["saved"] = round(l["saved"] * factor, 2)
        breakdown = {k: round(v * factor, 2) for k, v in breakdown.items()}
        total = round(sum(l["saved"] for l in line_results), 2)

    return {
        "discount": total,
        "membership": membership["name"],
        "plan": plan.name,
        "free_delivery": 1 if plan.free_delivery else 0,
        "free_delivery_min_order": flt(plan.free_delivery_min_order),
        "lines": line_results,
        "breakdown": [{"label": k, "amount": v} for k, v in breakdown.items()],
    }


# ── Ledger ───────────────────────────────────────────────────────────────────

def record_savings(order_doc):
    """Write one Membership Saving Entry per rule used, after the order exists.

    Idempotent on order name: Order.on_update fires repeatedly through the
    delivery lifecycle, and without this guard every status change would
    inflate the customer's lifetime savings.
    """
    if not order_doc.get("membership") or flt(order_doc.get("membership_discount")) <= 0:
        return

    if frappe.db.exists("Membership Saving Entry", {"order": order_doc.name}):
        return

    # Must re-resolve against the SAME figure totals.py used — net total less
    # the discounts applied before membership. Passing the bare net_total here
    # would let a rule whose min_order_amount sits between the two values into
    # the ledger even though the customer was never given it, so the entries
    # would sum to more than membership_discount actually charged.
    net_after = (
        flt(order_doc.get("net_total"))
        - flt(order_doc.get("coupon_discount"))
        - flt(order_doc.get("onboarding_discount"))
    )
    result = resolve_membership_discount(
        order_doc.get("items") or [],
        order_doc.get("customer_email"),
        net_after,
    )

    for row in result["breakdown"]:
        frappe.get_doc({
            "doctype": "Membership Saving Entry",
            "customer_email": order_doc.get("customer_email"),
            "membership": order_doc.get("membership"),
            "order": order_doc.name,
            "benefit_label": row["label"],
            "amount_saved": row["amount"],
        }).insert(ignore_permissions=True)

    total = frappe.db.sql(
        """select coalesce(sum(amount_saved), 0) from `tabMembership Saving Entry`
           where membership = %s""",
        (order_doc.get("membership"),),
    )[0][0]
    frappe.db.set_value("Customer Membership", order_doc.get("membership"), "total_saved", flt(total))


# ── Scheduled ────────────────────────────────────────────────────────────────

def expire_memberships():
    """Daily — flip lapsed memberships to Expired.

    The resolver already filters on expires_on, so this is bookkeeping for the
    admin list and renewal prompts, not a correctness guard.
    """
    lapsed = frappe.get_all(
        "Customer Membership",
        filters={"status": "Active", "expires_on": ["<", nowdate()]},
        pluck="name",
    )
    for name in lapsed:
        frappe.db.set_value("Customer Membership", name, "status", "Expired")
    if lapsed:
        frappe.db.commit()
    return len(lapsed)


# ── Whitelisted ──────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_my_membership():
    """Current membership + lifetime savings, for the account page."""
    email = frappe.session.user
    membership = get_active_membership(email)
    if not membership:
        return {"active": False, "plans": list_plans()}

    doc = frappe.get_doc("Customer Membership", membership["name"])
    plan = frappe.get_cached_doc("Membership Plan", doc.plan)
    return {
        "active": True,
        "plan": plan.plan_name,
        "tagline": plan.tagline or "",
        "expires_on": str(doc.expires_on),
        "total_saved": flt(doc.total_saved),
        "free_delivery": bool(plan.free_delivery),
        "benefits": [
            {"label": _benefit_label(b.as_dict()), "scope": b.scope}
            for b in (plan.benefits or []) if b.is_active
        ],
    }


@frappe.whitelist(allow_guest=True)
def list_plans():
    """Plans for the storefront's membership page."""
    plans = frappe.get_all(
        "Membership Plan",
        filters={"is_active": 1},
        fields=["name", "plan_name", "tagline", "price", "duration_days",
                "free_delivery", "free_delivery_min_order"],
        order_by="sort_order asc, price asc",
    )
    for plan in plans:
        doc = frappe.get_cached_doc("Membership Plan", plan["name"])
        plan["benefits"] = [
            {"label": _benefit_label(b.as_dict()), "scope": b.scope}
            for b in (doc.benefits or []) if b.is_active
        ]
    return plans
