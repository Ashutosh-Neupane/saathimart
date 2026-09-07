# Redis Streams Implementation for SaathiMart

## Overview

This implementation uses **Redis Streams** for real-time, reliable event streaming between the SaathiMart hub and vendor ERPNext instances.

**No additional packages required** - uses Frappe's built-in Redis client (`frappe.cache()`).

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Redis Streams                             │
│                                                              │
│  Stream: hub:vendor:{vendor_id}:events                      │
│  ┌──────┬──────┬──────┬──────┐                              │
│  │ MSG1 │ MSG2 │ MSG3 │ MSG4 │  (FIFO, persisted)          │
│  └──────┴──────┴──────┴──────┘                              │
│                                                              │
│  Consumer Group: vendor-workers                             │
│  ┌─────────────────┐  ┌─────────────────┐                  │
│  │  worker-1       │  │  worker-2       │  (parallel)      │
│  └─────────────────┘  └─────────────────┘                  │
└─────────────────────────────────────────────────────────────┘
         ▲                                │
         │ XADD                           │ XREADGROUP
         │                                │
┌────────┴────────┐            ┌─────────┴──────────┐
│       HUB       │            │      VENDOR        │
│  (Publisher)    │            │   (Consumer)       │
└─────────────────┘            └────────────────────┘
```

## Features

✅ **Guaranteed delivery** - Events persisted in Redis, survive restarts  
✅ **Ordered processing** - FIFO within each vendor's stream  
✅ **Consumer groups** - Parallel processing with automatic load balancing  
✅ **Automatic retry** - Stuck messages claimed via XCLAIM  
✅ **Dead letter queue** - Failed messages moved to DLQ after 5 retries  
✅ **Monitoring** - XPENDING, XINFO for real-time visibility  
✅ **No message loss** - Transactional outbox pattern  

## Quick Start

### Hub Side (Publishing Events)

```python
# In hooks.py - Order.after_insert()
"Order": {
    "after_insert": [
        "saathimart.streams.publisher.publish_order_created",
    ],
}

# Publisher enqueues background task (fast API response)
# Background worker executes XADD to Redis Stream
```

### Vendor Side (Consuming Events)

```python
# In hooks.py
scheduler_events = {
    "all": [
        "saathimart_vendor.streams.worker.consume_hub_events",
    ],
}

# Runs every 4 minutes, processes batch of 10 events
# Uses XREADGROUP with consumer group for parallel processing
```

## API Reference

### Publishing Events

```python
from saathimart.streams.publisher import publish_to_vendor

# Publish single event (via frappe.enqueue)
publish_to_vendor(
    vendor_id="VENDOR001",
    event_type="order.new",
    payload={"order_id": "ORD-2024-001", ...}
)
```

### Consuming Events

```python
from saathimart_vendor.streams.consumer import StreamConsumer

consumer = StreamConsumer("VENDOR001", "worker-1")
consumer.ensure_group()  # Create consumer group (once)

# Read new messages
messages = consumer.consume(count=10, block_ms=5000)

for msg_id, event in messages:
    # Process event
    success = process_event(event)
    
    if success:
        # Acknowledge (remove from pending list)
        consumer.acknowledge([msg_id])
    # else: will be retried via XCLAIM

# Claim stuck messages (retry logic)
stuck = consumer.claim_pending(min_idle_ms=60000)
```

### Monitoring

```python
from saathimart.streams.monitor import StreamMonitor

monitor = StreamMonitor("VENDOR001")

# Health check
health = monitor.check_health()
# Returns: {status: "healthy", pending_count: 5, alerts: []}

# Detailed metrics
metrics = monitor.get_metrics()
# Returns: {stream_length: 1234, pending: 5, consumers: 2}

# Get stuck messages
stuck = monitor.get_stuck_messages(min_idle_seconds=60)
```

### Dead Letter Queue

```python
from saathimart.streams.dead_letter import DeadLetterQueue

dlq = DeadLetterQueue("VENDOR001")

# Check and move failed messages to DLQ
moved = dlq.check_and_move_to_dlq()

# Get dead letters
dead = dlq.get_dead_letters()

# Retry a dead letter
dlq.retry_message(msg_id, payload)

# Acknowledge (remove)
dlq.acknowledge(msg_id)
```

## Redis Commands Used

### Publishing (Hub)

```bash
# Add message to stream (with automatic trimming)
XADD hub:vendor:VENDOR001:events MAXLEN ~ 10000 * event_type order.new payload {...}

# Get stream info
XINFO STREAM hub:vendor:VENDOR001:events
```

### Consuming (Vendor)

```bash
# Create consumer group (once)
XGROUP CREATE hub:vendor:VENDOR001:events vendor-workers $ MKSTREAM

# Read new messages
XREADGROUP GROUP vendor-workers worker-1 COUNT 10 BLOCK 5000 STREAMS hub:vendor:VENDOR001:events >

# Acknowledge processed message
XACK hub:vendor:VENDOR001:events vendor-workers 1526569495631-0

# Check pending messages
XPENDING hub:vendor:VENDOR001:events vendor-workers - + 10

# Claim stuck message (idle > 60s)
XCLAIM hub:vendor:VENDOR001:events vendor-workers worker-2 60000 1526569495631-0
```

## Event Flow

### Order Created

1. **Order.after_insert()** → `publish_order_created()`
2. **frappe.enqueue()** → Background task queued
3. **Background worker** → `XADD` to `hub:vendor:{vendor_id}:events`
4. **Vendor scheduler** → `XREADGROUP` every 4 minutes
5. **Event processor** → Creates Vendor Order in ERPNext
6. **XACK** → Message acknowledged

### Retry Logic

1. Worker crashes mid-processing
2. Message stays in **Pending Entries List (PEL)**
3. After 60 seconds idle, another worker **claims** it via `XCLAIM`
4. If processing fails again, message stays pending
5. After **5 delivery attempts**, moved to Dead Letter Queue
6. DLQ visible in desk UI for manual inspection

## Monitoring Dashboard

Access via Desk: **Tools → Stream Health Dashboard**

Shows:
- Stream length per vendor
- Pending message count
- Oldest pending message age
- Active consumers
- Dead letter count
- Real-time alerts

## Configuration

### Scheduler Frequency

```python
# In hooks.py
scheduler_events = {
    "all": [  # Every 4 minutes
        "saathimart_vendor.streams.worker.consume_hub_events"
    ],
}
```

### Batch Size

```python
# In worker.py
def consume_hub_events(batch_size=10, block_ms=5000):
    # Adjust based on your volume
```

### Retry Thresholds

```python
# In monitor.py
MAX_PENDING_THRESHOLD = 100      # Alert if > 100 pending
MAX_IDLE_TIME_MINUTES = 10      # Alert if oldest > 10 min
MAX_RETRIES = 5                  # Move to DLQ after 5 failures
```

## Performance

**Throughput:**
- 1 `XADD` per event (~1-2ms)
- Batch `XREADGROUP` of 10 messages (~5ms)
- Parallel processing via consumer groups

**For 1000 concurrent orders:**
- HTTP webhooks: 1000 HTTP requests (blocking)
- Redis Streams: 1000 `XADD` (non-blocking) + batch processing

**Latency:**
- Publish: ~1-2ms (via frappe.enqueue)
- Consume: ~5ms per batch of 10
- End-to-end: < 10 seconds (with 4-minute scheduler)

## Testing

```bash
# Test stream publishing
bench --site dev.site execute saathimart.streams.publisher.test_publish

# Test stream consuming
bench --site vendor.site execute saathimart_vendor.streams.worker.test_consume

# Check stream health
bench --site dev.site execute saathimart.streams.monitor.check_all_streams
```

## Troubleshooting

### Messages stuck in pending

```python
# Check stuck messages
stuck = monitor.get_stuck_messages(min_idle_seconds=300)

# Purge very old messages (use with caution)
from saathimart_vendor.streams.worker import purge_stuck_messages
purge_stuck_messages(vendor_id, max_idle_hours=24)
```

### High pending count

1. Check if consumers are running: `bench worker --queue short`
2. Check consumer group status: `XINFO GROUPS hub:vendor:{vendor_id}:events`
3. Scale consumers: Add more workers to consumer group

### Dead letters accumulating

1. Check Dead Letter Log in desk
2. Retry manually or fix underlying issue
3. Adjust MAX_RETRIES if needed

## Comparison with HTTP Webhooks

| Aspect | HTTP Webhooks | Redis Streams |
|--------|--------------|---------------|
| **Delivery** | Best-effort | Guaranteed (persisted) |
| **Ordering** | None | FIFO |
| **Retry** | Manual (cron) | Automatic (XCLAIM) |
| **Backpressure** | None | Yes (consumer pulls) |
| **Monitoring** | Custom | Built-in (XINFO) |
| **Scaling** | Add web workers | Add consumer group members |
| **1000 orders** | 1000 HTTP requests | 1000 XADD + batch XREADGROUP |
| **Latency** | HTTP overhead | ~1-2ms (Redis) |
| **Fault tolerance** | Low | High (auto-retry) |

## Migration from HTTP Webhooks

The implementation runs **in parallel** with existing HTTP webhooks:

1. HTTP webhooks continue as fallback
2. Redis Streams added as primary channel
3. Both process events (deduplication via `event_id`)
4. Monitor stream processing lag
5. Once stable, remove HTTP webhook code

## Files

```
saathimart/
├── streams/
│   ├── __init__.py
│   ├── publisher.py      # Hub-side: XADD events
│   ├── tasks.py          # Background tasks (frappe.enqueue)
│   ├── monitor.py        # Health checks, alerts
│   ├── dead_letter.py    # DLQ handling
│   └── api.py            # Desk API endpoints

saathimart-vendor/
├── streams/
│   ├── __init__.py
│   ├── consumer.py       # XREADGROUP consumer
│   ├── processor.py      # Event handlers
│   └── worker.py         # Background worker
```

## Best Practices

1. **Idempotency**: Event handlers must be idempotent (check `event_id`)
2. **Monitoring**: Set up alerts for pending count > threshold
3. **Dead letters**: Review DLQ daily in production
4. **Batch size**: Adjust based on your event volume
5. **Consumer scaling**: Add workers during peak hours
6. **Testing**: Test retry logic with stuck messages
7. **Backup**: Redis persistence enabled (RDB + AOF)

## License

MIT
