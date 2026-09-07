# -*- coding: utf-8 -*-
# Copyright (c) 2024, Trevo Cloud Nepal and contributors
# For license information, see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class DeadLetterLog(Document):
    def before_save(self):
        if self.status == "Retried" and not self.retry_msg_id:
            frappe.throw(_("Retry Message ID is required when status is Retried"))
    
    def on_update(self):
        if self.has_value_changed("status") and self.status == "Resolved":
            self.resolved_at = frappe.utils.now_datetime()
            self.resolved_by = frappe.session.user
    
    @frappe.whitelist()
    def retry(self):
        """Retry this dead letter message."""
        from saathimart.streams.dead_letter import DeadLetterQueue
        import json
        
        dlq = DeadLetterQueue(self.vendor_id)
        
        success = dlq.retry_message(
            self.original_msg_id,
            {
                "event_type": self.event_type,
                "payload": self.payload,
            }
        )
        
        if success:
            self.status = "Retried"
            self.resolved_at = frappe.utils.now_datetime()
            self.resolved_by = frappe.session.user
            self.resolution_notes = (self.resolution_notes or "") + "\nRetried successfully."
            self.save()
            return {"success": True}
        
        return {"success": False, "error": "Failed to retry message"}
    
    @frappe.whitelist()
    def ignore(self, reason: str = ""):
        """Mark this dead letter as ignored (will not be retried)."""
        self.status = "Ignored"
        self.resolved_at = frappe.utils.now_datetime()
        self.resolved_by = frappe.session.user
        self.resolution_notes = (self.resolution_notes or "") + f"\nIgnored: {reason}"
        self.save()
