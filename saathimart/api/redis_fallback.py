"""
Graceful degradation when Redis is unavailable.

All Redis-backed features (rate limiter, circuit breaker, cache) fall back
to MariaDB-based alternatives so the app doesn't break when Redis is down.

Usage:
    from saathimart.api.redis_fallback import get_cache_fallback
    cache = get_cache_fallback()
    cache.set_value("key", "value", expires_in_sec=60)
"""
import frappe
from frappe.utils import now_datetime, add_to_date, get_datetime


class DBCacheFallback:
    """MariaDB-backed cache that mimics Redis cache interface.

    Uses a single table `sm_cache_fallback` with TTL support.
    Created on first use if it doesn't exist.
    """
    _TABLE = "sm_cache_fallback"

    def _ensure_table(self):
        if frappe.db.exists("DocType", "sm_cache_fallback"):
            return
        frappe.db.sql(f"""
            CREATE TABLE IF NOT EXISTS `{self._TABLE}` (
                `key` VARCHAR(255) NOT NULL PRIMARY KEY,
                `value` MEDIUMTEXT,
                `expires_at` DATETIME,
                `INDEX idx_expires` (`expires_at`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        frappe.db.commit()

    def get_value(self, key, default=None):
        self._ensure_table()
        row = frappe.db.sql(
            f"SELECT value, expires_at FROM `{self._TABLE}` WHERE `key` = %s",
            (key,),
            as_dict=True,
        )
        if not row:
            return default
        if row[0].expires_at and get_datetime(row[0].expires_at) < now_datetime():
            frappe.db.delete(self._TABLE, {"key": key})
            return default
        try:
            import json
            return json.loads(row[0].value)
        except (TypeError, ValueError):
            return row[0].value

    def set_value(self, key, value, expires_in_sec=None):
        self._ensure_table()
        import json
        val = json.dumps(value) if not isinstance(value, str) else value
        expires_at = None
        if expires_in_sec:
            expires_at = add_to_date(now_datetime(), seconds=expires_in_sec)
        frappe.db.sql(f"""
            INSERT INTO `{self._TABLE}` (`key`, `value`, `expires_at`)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE `value` = VALUES(`value`), `expires_at` = VALUES(`expires_at`)
        """, (key, val, expires_at))
        frappe.db.commit()

    def delete_key(self, key):
        self._ensure_table()
        frappe.db.delete(self._TABLE, {"key": key})
        frappe.db.commit()


def get_cache_fallback():
    """Return Redis cache if available, else MariaDB fallback."""
    try:
        cache = frappe.cache()
        # Test Redis connectivity
        cache.set_value("_sm_ping", 1, expires_in_sec=5)
        if cache.get_value("_sm_ping") == 1:
            return cache
    except Exception:
        pass
    return DBCacheFallback()


def safe_cache_get(key, default=None):
    """Get a value from cache, falling back gracefully."""
    try:
        return frappe.cache().get_value(key) or default
    except Exception:
        try:
            return DBCacheFallback().get_value(key, default)
        except Exception:
            return default


def safe_cache_set(key, value, expires_in_sec=60):
    """Set a value in cache, falling back gracefully."""
    try:
        frappe.cache().set_value(key, value, expires_in_sec=expires_in_sec)
    except Exception:
        try:
            DBCacheFallback().set_value(key, value, expires_in_sec=expires_in_sec)
        except Exception:
            pass
