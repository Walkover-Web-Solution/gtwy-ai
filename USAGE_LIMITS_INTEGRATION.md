# Usage Limits System Integration Guide

## Overview

This document describes how to integrate the usage limits system into your gtwy.ai middleware. The system enforces per-bridge, per-folder, and per-API-key usage limits while maintaining accurate billing records.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Python Middleware (FastAPI)                                     │
├─────────────────────────────────────────────────────────────────┤
│ 1. Request arrives with X-Org-ID, X-Bridge-ID headers          │
│ 2. UsageLimitsMiddleware checks Redis for current usage         │
│ 3. Lua script atomically checks all 3 limits & reserves cost    │
│ 4. If allowed: proceed; if rejected: return 429                 │
│ 5. After LLM call: settle actual cost vs reservation            │
│ 6. Publish usage event to RabbitMQ (fire-and-forget)            │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ RabbitMQ (Message Queue)                                        │
├─────────────────────────────────────────────────────────────────┤
│ Queue: usage_events (durable)                                   │
│ Messages: JSON usage events with request_id, cost, tokens       │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ Node.js Consumer                                                │
├─────────────────────────────────────────────────────────────────┤
│ 1. Batch up to 1000 events or wait 1 second                     │
│ 2. Write batch to TimescaleDB in one transaction                │
│ 3. Use request_id as unique constraint (idempotent)             │
│ 4. Acknowledge messages only after successful write              │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ TimescaleDB (Billing Source of Truth)                           │
├─────────────────────────────────────────────────────────────────┤
│ Table: usage_events (hypertable, partitioned by timestamp)      │
│ View: daily_usage_summary (for dashboards)                      │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ Reconciliation Job (Every 5 minutes)                            │
├─────────────────────────────────────────────────────────────────┤
│ Compare Redis counters vs TimescaleDB totals                    │
│ If difference > 10%: correct Redis from TimescaleDB             │
└─────────────────────────────────────────────────────────────────┘
```

## Step-by-Step Integration

### Step 1: Environment Variables

Add to `.env`:

```bash
# RabbitMQ
QUEUE_CONNECTIONURL=amqp://user:password@localhost:5672
USAGE_EVENTS_QUEUE_NAME=usage_events
USAGE_EVENTS_FAILED_QUEUE_NAME=usage_events_failed

# Redis (already configured)
REDIS_URI=redis://localhost:6379

# TimescaleDB (already configured)
DB_HOST=localhost
DB_USER=postgres
DB_PASS=password
DB_NAME=timescale_db
```

### Step 2: Initialize Services in Python

In your main FastAPI app initialization:

```python
from src.services.usage_limits_service import usage_limits_service
from src.services.usage_events_producer import initialize_producer, close_producer
from src.middlewares.usage_limits_middleware import UsageLimitsMiddleware

@app.on_event("startup")
async def startup():
    # Initialize usage limits service
    await usage_limits_service.initialize()
    
    # Initialize RabbitMQ producer
    await initialize_producer()

@app.on_event("shutdown")
async def shutdown():
    # Close RabbitMQ connection
    await close_producer()

# Add middleware (after other middlewares)
app.add_middleware(UsageLimitsMiddleware)
```

### Step 3: Run TimescaleDB Migration

```bash
npm run migrateTimescale:up
```

This creates:
- `usage_events` hypertable (partitioned by timestamp)
- Indexes on org_id, bridge_id, folder_id, apikey_id, service
- `daily_usage_summary` materialized view for dashboards

### Step 4: Configure MongoDB Limits

Insert limit configuration into MongoDB `bridges` collection:

```javascript
db.bridges.updateOne(
  { _id: "bridge_123" },
  {
    $set: {
      org_id: "org_456",
      bridge_limit: 100.0,
      bridge_reset_period: "monthly",
      bridge_start_date: ISODate("2024-01-15T10:30:00Z"),
      bridge_hard_stop: true,
      
      folder_limit: 500.0,
      folder_reset_period: "monthly",
      folder_start_date: ISODate("2024-01-15T10:30:00Z"),
      folder_hard_stop: false,
      
      apikey_limit: 1000.0,
      apikey_reset_period: "monthly",
      apikey_start_date: ISODate("2024-01-15T10:30:00Z"),
      apikey_hard_stop: true
    }
  }
)
```

### Step 5: Update Request Headers

Client requests must include:

```
X-Org-ID: org_456
X-Bridge-ID: bridge_123
X-Folder-ID: folder_789 (optional)
X-API-Key-ID: key_abc (optional)
```

### Step 6: Integrate into Your LLM Call Handler

Example in your controller:

```python
from src.services.usage_limits_service import usage_limits_service
from src.services.usage_events_producer import publish_usage_event
from src.utils.token_cost_calculator import estimate_cost, calculate_actual_cost

async def call_llm(request: Request, prompt: str):
    org_id = request.state.usage_limits["org_id"]
    bridge_id = request.state.usage_limits["bridge_id"]
    folder_id = request.state.usage_limits["folder_id"]
    apikey_id = request.state.usage_limits["apikey_id"]
    request_id = request.state.request_id
    
    # Estimate cost (worst case) - pricing fetched from model_config_document
    estimated_cost = estimate_cost(
        service="openai",
        model="gpt-4",
        max_tokens=2000,
        input_tokens=len(prompt.split())
    )
    
    # Call LLM
    response = await openai.ChatCompletion.acreate(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000
    )
    
    # Calculate actual cost
    tokens_in = response.usage.prompt_tokens
    tokens_out = response.usage.completion_tokens
    actual_cost = calculate_actual_cost(
        service="openai",
        model="gpt-4",
        tokens_in=tokens_in,
        tokens_out=tokens_out
    )
    
    # Settle the difference
    await usage_limits_service.settle_usage(
        org_id=org_id,
        bridge_id=bridge_id,
        folder_id=folder_id,
        apikey_id=apikey_id,
        reservation_cost=estimated_cost,
        actual_cost=actual_cost
    )
    
    # Publish usage event for billing
    await publish_usage_event(
        request_id=request_id,
        org_id=org_id,
        bridge_id=bridge_id,
        service="openai",
        model="gpt-4",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=actual_cost,
        status="success",
        folder_id=folder_id,
        apikey_id=apikey_id,
        reservation_cost=estimated_cost,
        actual_cost=actual_cost
    )
    
    return response
```

### Step 7: Set Up Reconciliation Job

Create a cron job that runs every 5 minutes:

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.services.usage_reconciliation_service import reconcile_all_entities

scheduler = AsyncIOScheduler()

@scheduler.scheduled_job('interval', minutes=5)
async def reconciliation_job():
    from models.mongo_connection import db
    
    orgs = await db["organizations"].find({}).to_list(None)
    for org in orgs:
        await reconcile_all_entities(org["_id"])

scheduler.start()
```

## Request Flow Example

### Scenario: Bridge at $99/month, limit is $100

**Request 1: Estimate $2**
1. Middleware checks Redis: bridge_usage = $99
2. Lua script: $99 + $2 = $101 > $100 → **REJECTED**
3. Return 429: "Bridge limit exceeded. $99 used of $100."

**Request 2: Estimate $0.50**
1. Middleware checks Redis: bridge_usage = $99
2. Lua script: $99 + $0.50 = $99.50 ≤ $100 → **ACCEPTED**
3. Reserve $0.50 in Redis: bridge_usage = $99.50
4. Call OpenAI, get actual cost = $0.30
5. Settle: adjust Redis by ($0.30 - $0.50) = -$0.20 → bridge_usage = $99.30
6. Publish event to RabbitMQ
7. Return 200 with response

**Background: Consumer writes to TimescaleDB**
1. Consumer batches 1000 events or waits 1 second
2. Writes batch in one transaction
3. TimescaleDB rejects duplicates (request_id is unique)
4. Acknowledges messages

**Every 5 minutes: Reconciliation**
1. Query TimescaleDB: total cost for bridge this month = $99.28
2. Query Redis: bridge_usage = $99.30
3. Difference = $0.02 (0.02%) → within 10% threshold
4. No correction needed

## Error Handling

### Redis Crash
- If Redis is down, middleware returns 503
- For paid tiers: rebuild counter from TimescaleDB
- For free tiers: reject all requests (safer)

### RabbitMQ Failure
- Message stays in queue, retried by consumer
- No double-billing (request_id is unique in TimescaleDB)

### Consumer Crash
- Messages stay in RabbitMQ
- Restarted consumer picks them up
- Idempotent write (ON CONFLICT DO NOTHING)

### Large Difference in Reconciliation
- If Redis and TimescaleDB differ by >10%
- Redis is corrected to match TimescaleDB
- Logged as warning for investigation

## Monitoring & Dashboards

### Key Metrics to Track

```sql
-- Current usage by bridge
SELECT bridge_id, SUM(cost_usd) as total_cost
FROM usage_events
WHERE org_id = 'org_456'
  AND timestamp >= DATE_TRUNC('month', NOW())
GROUP BY bridge_id;

-- Daily usage trend
SELECT DATE_TRUNC('day', timestamp) as day, SUM(cost_usd) as daily_cost
FROM usage_events
WHERE org_id = 'org_456'
GROUP BY DATE_TRUNC('day', timestamp)
ORDER BY day DESC;

-- Top models by cost
SELECT service, model, COUNT(*) as requests, SUM(cost_usd) as total_cost
FROM usage_events
WHERE org_id = 'org_456'
GROUP BY service, model
ORDER BY total_cost DESC;
```

### Alert Thresholds

```python
# Alert when usage crosses 50%, 80%, 100%
ALERT_THRESHOLDS = [0.5, 0.8, 1.0]

async def check_and_alert(org_id, bridge_id, limit, current_usage):
    percentage = current_usage / limit if limit > 0 else 0
    for threshold in ALERT_THRESHOLDS:
        if percentage >= threshold:
            await send_alert(
                org_id=org_id,
                bridge_id=bridge_id,
                message=f"Usage at {percentage*100:.0f}% of limit"
            )
```

## Pricing Configuration

The system fetches pricing dynamically from `model_config_document` (MongoDB `modelconfigurations` collection). This ensures pricing is always up-to-date without code changes.

**Model Configuration Structure**:
```javascript
{
  service: "openai",
  model_name: "gpt-4",
  outputConfig: {
    usage: [
      {
        inputPrice: 0.03,    // Price per 1000 input tokens
        outputPrice: 0.06    // Price per 1000 output tokens
      }
    ]
  }
}
```

**How It Works**:
1. `model_config_document` is loaded from MongoDB on startup
2. `estimate_cost()` and `calculate_actual_cost()` fetch pricing from this document
3. Pricing updates automatically when MongoDB changes (via change stream)
4. No code deployment needed for pricing updates

**Adding New Models**:
Simply insert a new document in MongoDB `modelconfigurations` collection with the pricing structure above. The system will automatically use it.

## Rollout Strategy

### Phase 1: Safe Additions (No Breaking Changes)
- ✅ Step 1: Add request_id generation
- ✅ Step 2: Set up TimescaleDB and RabbitMQ consumer
- ✅ Step 3: Start writing usage events (no reading yet)

### Phase 2: Feature Flag Rollout
- ✅ Step 4: Enable Lua script check-and-reserve for 10% of orgs
- ✅ Step 5: Monitor error rates, compare with old code path
- ✅ Step 6: Gradually increase to 50%, then 100%

### Phase 3: Cleanup
- ✅ Step 7: Stop writing to MongoDB usage fields
- ✅ Step 8: Add hard_stop config and soft-limit warnings
- ✅ Step 9: Surface in dashboard

## Testing

### Unit Tests

```python
# Test Lua script atomicity
async def test_concurrent_requests_at_limit():
    # Simulate two requests at 99% usage
    # Only one should succeed
    pass

# Test settlement
async def test_settlement_adjustment():
    # Reserve $2, actual cost $0.50
    # Verify adjustment is -$1.50
    pass

# Test reconciliation
async def test_reconciliation_corrects_drift():
    # Set Redis to $100, TimescaleDB to $95
    # Run reconciliation
    # Verify Redis corrected to $95
    pass
```

### Integration Tests

```bash
# 1. Start all services
docker-compose up -d

# 2. Run migrations
npm run migrateTimescale:up

# 3. Insert test limits
mongosh < test_limits.js

# 4. Send test requests
curl -X POST http://localhost:8000/api/chat \
  -H "X-Org-ID: test_org" \
  -H "X-Bridge-ID: test_bridge" \
  -H "X-Estimated-Cost: 0.05" \
  -d '{"prompt": "Hello"}'

# 5. Verify in TimescaleDB
psql -c "SELECT * FROM usage_events ORDER BY timestamp DESC LIMIT 10"

# 6. Check Redis counters
redis-cli GET "AIMIDDLEWARE_dev_quota:test_org:bridge:test_bridge:monthly:2024-01-15T10:30:00"
```

## Troubleshooting

### Issue: 429 errors when limit should allow

**Cause**: Redis counter is stale or incorrect

**Fix**:
```bash
# Clear Redis counter to force rebuild from TimescaleDB
redis-cli DEL "AIMIDDLEWARE_dev_quota:org_id:bridge:bridge_id:monthly:start_date"

# Or run reconciliation manually
python -c "
import asyncio
from src.services.usage_reconciliation_service import reconcile_all_entities
asyncio.run(reconcile_all_entities('org_id'))
"
```

### Issue: Usage events not appearing in TimescaleDB

**Cause**: Consumer not running or RabbitMQ queue not configured

**Fix**:
```bash
# Check consumer logs
docker logs middleware-node

# Check RabbitMQ queue
rabbitmqctl list_queues usage_events

# Manually trigger consumer
node src/consumers/index.js
```

### Issue: Large difference between Redis and TimescaleDB

**Cause**: Possible data loss or calculation error

**Fix**:
```bash
# Check TimescaleDB for missing events
psql -c "
SELECT COUNT(*) FROM usage_events
WHERE timestamp >= NOW() - INTERVAL '1 day'
"

# Check Redis for stale keys
redis-cli KEYS "AIMIDDLEWARE_*quota*"

# Run reconciliation with logging
python -c "
import logging
logging.basicConfig(level=logging.DEBUG)
import asyncio
from src.services.usage_reconciliation_service import reconcile_all_entities
asyncio.run(reconcile_all_entities('org_id'))
"
```

## Performance Considerations

### Redis Lua Script
- Runs atomically inside Redis (no interleaving)
- Typical execution: <1ms
- Handles ~1M concurrent requests without slowdown

### TimescaleDB Batch Writes
- 1000 events per batch
- ~1 second flush interval
- Uses COPY for fast bulk insert
- Typical write: <100ms for 1000 rows

### Reconciliation Job
- Runs every 5 minutes
- Queries TimescaleDB (indexed by timestamp)
- Updates Redis only if difference >10%
- Typical runtime: <5 seconds for 1000 bridges

## Security Considerations

1. **Request ID**: Prevents double-billing on retries
2. **Lua Script**: Atomic check-and-reserve prevents race conditions
3. **Unique Constraint**: TimescaleDB enforces idempotency
4. **Hard Limits**: Block requests that exceed limits
5. **Audit Trail**: All usage recorded in TimescaleDB with timestamps

## References

- Redis Lua Scripting: https://redis.io/commands/eval/
- TimescaleDB Hypertables: https://docs.timescale.com/use-timescale/latest/hypertables/
- RabbitMQ Reliability: https://www.rabbitmq.com/reliability.html
