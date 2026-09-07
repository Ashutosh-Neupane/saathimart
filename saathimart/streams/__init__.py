"""
Redis Streams implementation for SaathiMart hub-vendor communication.

Uses Frappe's built-in Redis client (frappe.cache()) to execute
Redis Streams commands. No additional packages required.

Architecture:
- Hub publishes events to vendor-specific streams
- Vendor consumers read from streams using consumer groups
- Automatic retry via XCLAIM for failed/stuck messages
- Monitoring via XPENDING/XINFO commands

Stream naming convention:
  hub:vendor:{vendor_id}:events - Hub → Vendor events
  vendor:{vendor_id}:hub:events - Vendor → Hub events (acknowledgments, sync)
"""
