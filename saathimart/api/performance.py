"""
Performance monitoring and logging for Saathimart.

Features:
- Slow query logging (queries > 1 second)
- API response time tracking
- Structured JSON logging with trace IDs
"""
import frappe
import json
import time
import logging
from frappe.utils import now_datetime
from datetime import datetime

# Configure JSON logger
class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Add trace ID if available
        if hasattr(record, 'trace_id'):
            log_entry["trace_id"] = record.trace_id
        
        # Add extra fields
        if hasattr(record, 'extra_fields'):
            log_entry.update(record.extra_fields)
        
        return json.dumps(log_entry)


def setup_json_logging():
    """Setup JSON logging handler for structured logs."""
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger('saathimart')
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


logger = setup_json_logging()


def log_request_start():
    """Log request start with trace ID."""
    trace_id = frappe.generate_hash(length=12)
    frappe.local.saathimart_trace_id = trace_id
    
    logger.info(
        "Request started",
        extra={
            'extra_fields': {
                'trace_id': trace_id,
                'method': frappe.request.method if frappe.request else None,
                'path': frappe.request.path if frappe.request else None,
                'ip': frappe.local.request_ip if hasattr(frappe.local, 'request_ip') else None,
            }
        }
    )
    return trace_id


def log_request_end(trace_id, duration_ms, status_code):
    """Log request completion."""
    logger.info(
        "Request completed",
        extra={
            'extra_fields': {
                'trace_id': trace_id,
                'duration_ms': duration_ms,
                'status_code': status_code,
            }
        }
    )


def log_slow_query(query, duration_ms, trace_id=None):
    """Log slow database queries."""
    if duration_ms > 1000:  # Log queries over 1 second
        logger.warning(
            "Slow query detected",
            extra={
                'extra_fields': {
                    'trace_id': trace_id,
                    'duration_ms': duration_ms,
                    'query': query[:500] if query else None,
                }
            }
        )


def track_api_response_time(fn):
    """Decorator to track API response time."""
    def wrapper(*args, **kwargs):
        start_time = time.time()
        trace_id = getattr(frappe.local, 'saathimart_trace_id', None)
        
        try:
            result = fn(*args, **kwargs)
            duration_ms = (time.time() - start_time) * 1000
            log_request_end(trace_id, duration_ms, 200)
            return result
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            log_request_end(trace_id, duration_ms, 500)
            raise
    
    return wrapper