import frappe
from frappe.model.document import Document


class SMNotificationDevice(Document):
    def validate(self):
        if self.fcm_token and len(self.fcm_token) < 20:
            frappe.throw("Invalid FCM token")
