"""Feature Flags API for Saathimart.

Enables gradual rollout and A/B testing of new features.
"""
import frappe
import hashlib
from frappe.utils import now_datetime, getdate


def is_feature_enabled(feature_key, user=None):
    """Check if a feature is enabled for the current context.

    Args:
        feature_key: The feature flag key
        user: Optional user to check (defaults to current user)

    Returns:
        bool: True if feature is enabled
    """
    if not user:
        user = frappe.session.user

    # Get feature flag
    flag = frappe.db.get_value(
        "SM Feature Flag",
        {"feature_key": feature_key, "is_active": 1},
        ["feature_name", "rollout_percentage", "start_date", "end_date", "condition"],
        as_dict=True
    )

    if not flag:
        return False

    # Check date range
    now = now_datetime()
    if flag.start_date and now < flag.start_date:
        return False
    if flag.end_date and now > flag.end_date:
        return False

    # Check rollout percentage using consistent hashing
    # This ensures the same user gets the same experience consistently
    user_hash = hashlib.md5((user or "").encode()).hexdigest()
    user_percentage = int(user_hash[:4], 16) / 65535 * 100

    if user_percentage > flag.rollout_percentage:
        return False

    # Check custom condition (safe evaluation — no eval())
    if flag.condition and flag.condition.strip() not in ("True", "1", "yes"):
        try:
            result = _safe_evaluate_condition(flag.condition, user)
            if not result:
                return False
        except Exception:
            frappe.log_error(
                f"Feature flag condition failed: {flag.feature_name}",
                "feature_flag_error"
            )
            return False

    return True


def _safe_evaluate_condition(condition, user):
    """Safely evaluate a feature flag condition without using eval().

    Supported condition formats:
        - "True" / "False" — always enabled/disabled
        - "user == 'user@email.com'" — exact user match
        - "user in ['user1', 'user2']" — user in list
        - "is_customer" — check if user has role
        - "day_of_week == 'Monday'" — day-based rollout

    Returns:
        bool: Result of the condition
    """
    condition = condition.strip()

    # Simple boolean literals
    if condition.lower() in ("true", "1", "yes"):
        return True
    if condition.lower() in ("false", "0", "no"):
        return False

    # User-based conditions
    if "user ==" in condition:
        # Extract the email/string value
        parts = condition.split("==")
        if len(parts) == 2:
            expected = parts[1].strip().strip("'\"")
            return user == expected

    if "user !=" in condition:
        parts = condition.split("!=")
        if len(parts) == 2:
            expected = parts[1].strip().strip("'\"")
            return user != expected

    if "user in [" in condition:
        # Extract list values
        start = condition.index("[")
        end = condition.index("]")
        values_str = condition[start+1:end]
        values = [v.strip().strip("'\"") for v in values_str.split(",")]
        return user in values

    # Role-based conditions
    if condition.startswith("has_role:"):
        role = condition.split(":", 1)[1].strip()
        return frappe.db.exists(
            "Has Role",
            {"parent": user, "role": role}
        ) is not None

    # Day-based conditions
    if "day_of_week" in condition:
        from datetime import datetime
        today = datetime.now().strftime("%A")
        if "==" in condition:
            expected = condition.split("==")[1].strip().strip("'\"")
            return today == expected

    # Default: treat as False for safety
    return False


def get_enabled_features(user=None):
    """Get all enabled feature flags for current user.

    Args:
        user: Optional user to check

    Returns:
        list: List of enabled feature keys
    """
    flags = frappe.db.get_all(
        "SM Feature Flag",
        filters={"is_active": 1},
        fields=["feature_key", "rollout_percentage", "start_date", "end_date", "condition"]
    )

    enabled = []
    for flag in flags:
        if is_feature_enabled(flag.feature_key, user):
            enabled.append(flag.feature_key)

    return enabled


@frappe.whitelist()
def check_feature(feature_key):
    """Check if a feature is enabled (for frontend).

    Args:
        feature_key: The feature flag key

    Returns:
        dict: {"enabled": bool}
    """
    return {"enabled": is_feature_enabled(feature_key)}


@frappe.whitelist()
def list_features():
    """List all feature flags.

    Returns:
        list: Feature flags with status
    """
    flags = frappe.db.get_all(
        "SM Feature Flag",
        fields=["feature_name", "feature_key", "is_active", "rollout_percentage", "start_date", "end_date"],
        order_by="creation desc"
    )
    return flags
