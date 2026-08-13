"""
Shared constants for Saathi Middleware.

Single source of truth for values used in multiple modules to prevent drift.
"""


VALID_ORDER_TRANSITIONS = {
	"Pending":            ["Confirmed", "Cancelled"],
	"Confirmed":          ["Preparing", "Cancelled"],
	"Preparing":          ["Out for Delivery", "Cancelled"],
	"Out for Delivery":   ["Delivered"],
	"Delivered":          [],
	"Cancelled":          [],
	"Refunded":           [],
}
