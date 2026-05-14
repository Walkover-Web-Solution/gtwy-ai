# Usage Limits System - Testing Guide

## Test Environment Setup

### Prerequisites
- Python 3.9+
- Node.js 18+
- Redis
- PostgreSQL with TimescaleDB extension
- RabbitMQ
- MongoDB

### Start Services

```bash
# Start all services with Docker
docker-compose up -d

# Verify services are running
docker-compose ps

# Check Redis
redis-cli ping
# Expected: PONG

# Check RabbitMQ
curl -u guest:guest http://localhost:15672/api/overview
# Expected: 200 OK

# Check PostgreSQL/TimescaleDB
psql -h localhost -U postgres -d timescale_db -c "SELECT version();"

# Check MongoDB
mongosh --eval "db.adminCommand('ping')"
```

## Unit Tests

### Test 1: Lua Script Atomicity

```python
# tests/test_usage_limits_lua.py

import pytest
import asyncio
from redis.asyncio import Redis
from src.services.usage_limits_service import LUA_CHECK_AND_RESERVE

@pytest.mark.asyncio
async def test_lua_script_rejects_over_limit():
    """Test that Lua script correctly rejects requests over limit."""
    client = Redis.from_url("redis://localhost:6379")
    
    # Load script
    script_sha = await client.script_load(LUA_CHECK_AND_RESERVE)
    
    # Set up: bridge at $99, limit is $100
    bridge_key = "test:quota:org:bridge:bridge1:monthly:2024-01-15"
    await client.set(bridge_key, "99")
    
    # Try to reserve $2 (would exceed limit)
    result = await client.evalsha(
        script_sha,
        3,
        bridge_key,      # KEYS[1]
        "",              # KEYS[2] (folder)
        "",              # KEYS[3] (apikey)
        100,             # ARGV[1] bridge_limit
        0,               # ARGV[2] folder_limit
        0,               # ARGV[3] apikey_limit
        2,               # ARGV[4] reservation
        3600             # ARGV[5] ttl
    )
    
    # Should be rejected
    assert result[0] == 0, "Request should be rejected"
    assert result[1] == "bridge", "Should indicate bridge limit"
    assert result[2] == 99, "Current usage should be $99"
    assert result[3] == 100, "Limit should be $100"
    
    # Verify Redis was not modified
    current = await client.get(bridge_key)
    assert float(current) == 99, "Usage should not change on rejection"
    
    await client.close()

@pytest.mark.asyncio
async def test_lua_script_accepts_under_limit():
    """Test that Lua script correctly accepts requests under limit."""
    client = Redis.from_url("redis://localhost:6379")
    
    script_sha = await client.script_load(LUA_CHECK_AND_RESERVE)
    
    # Set up: bridge at $99, limit is $100
    bridge_key = "test:quota:org:bridge:bridge1:monthly:2024-01-15"
    await client.set(bridge_key, "99")
    
    # Try to reserve $0.50 (within limit)
    result = await client.evalsha(
        script_sha,
        3,
        bridge_key,
        "",
        "",
        100,
        0,
        0,
        0.5,
        3600
    )
    
    # Should be accepted
    assert result[0] == 1, "Request should be accepted"
    
    # Verify Redis was updated
    current = await client.get(bridge_key)
    assert float(current) == 99.5, "Usage should be incremented to $99.50"
    
    await client.close()

@pytest.mark.asyncio
async def test_lua_script_concurrent_requests():
    """Test that Lua script handles concurrent requests atomically."""
    client = Redis.from_url("redis://localhost:6379")
    
    script_sha = await client.script_load(LUA_CHECK_AND_RESERVE)
    
    bridge_key = "test:quota:org:bridge:bridge1:monthly:2024-01-15"
    await client.set(bridge_key, "99")
    
    # Simulate two concurrent requests at 99% usage
    async def make_request(reservation):
        return await client.evalsha(
            script_sha,
            3,
            bridge_key,
            "",
            "",
            100,
            0,
            0,
            reservation,
            3600
        )
    
    # Run both concurrently
    results = await asyncio.gather(
        make_request(0.75),
        make_request(0.75)
    )
    
    # Only one should succeed
    accepted = sum(1 for r in results if r[0] == 1)
    rejected = sum(1 for r in results if r[0] == 0)
    
    assert accepted == 1, "Only one request should be accepted"
    assert rejected == 1, "One request should be rejected"
    
    # Final usage should be $99.75
    current = await client.get(bridge_key)
    assert float(current) == 99.75, "Final usage should be $99.75"
    
    await client.close()
```

### Test 2: Settlement Logic

```python
# tests/test_usage_settlement.py

import pytest
from src.services.usage_limits_service import usage_limits_service

@pytest.mark.asyncio
async def test_settle_usage_refunds_reservation():
    """Test that settle_usage correctly refunds unused reservation."""
    await usage_limits_service.initialize()
    
    # Reserve $2, actual cost $0.50
    await usage_limits_service.settle_usage(
        org_id="test_org",
        bridge_id="test_bridge",
        folder_id=None,
        apikey_id=None,
        reservation_cost=2.0,
        actual_cost=0.5
    )
    
    # Adjustment should be -$1.50
    # (This would be verified by checking Redis counter)
    # In a real test, you'd check the Redis value

@pytest.mark.asyncio
async def test_settle_usage_charges_overage():
    """Test that settle_usage charges if actual cost exceeds reservation."""
    await usage_limits_service.initialize()
    
    # Reserve $0.50, actual cost $1.50 (overestimate)
    await usage_limits_service.settle_usage(
        org_id="test_org",
        bridge_id="test_bridge",
        folder_id=None,
        apikey_id=None,
        reservation_cost=0.5,
        actual_cost=1.5
    )
    
    # Adjustment should be +$1.00
```

### Test 3: Token Cost Calculation

```python
# tests/test_token_cost_calculator.py

import pytest
from src.utils.token_cost_calculator import estimate_cost, calculate_actual_cost

def test_estimate_cost_openai_gpt4():
    """Test cost estimation for OpenAI GPT-4."""
    cost = estimate_cost(
        service="openai",
        model="gpt-4",
        max_tokens=2000,
        input_tokens=100
    )
    
    # GPT-4: input $0.03/1k, output $0.06/1k
    # Input: 100 * 0.03 / 1000 = $0.003
    # Output: 2000 * 0.06 / 1000 = $0.12
    # Total: $0.123
    assert cost == pytest.approx(0.123, rel=0.01)

def test_calculate_actual_cost_openai_gpt4():
    """Test actual cost calculation for OpenAI GPT-4."""
    cost = calculate_actual_cost(
        service="openai",
        model="gpt-4",
        tokens_in=150,
        tokens_out=500
    )
    
    # Input: 150 * 0.03 / 1000 = $0.0045
    # Output: 500 * 0.06 / 1000 = $0.03
    # Total: $0.0345
    assert cost == pytest.approx(0.0345, rel=0.01)

def test_estimate_cost_anthropic_claude():
    """Test cost estimation for Anthropic Claude."""
    cost = estimate_cost(
        service="anthropic",
        model="claude-3-sonnet",
        max_tokens=1000,
        input_tokens=50
    )
    
    # Claude-3-Sonnet: input $0.003/1k, output $0.015/1k
    # Input: 50 * 0.003 / 1000 = $0.00015
    # Output: 1000 * 0.015 / 1000 = $0.015
    # Total: $0.01515
    assert cost == pytest.approx(0.01515, rel=0.01)

def test_unknown_model_returns_default():
    """Test that unknown model returns default cost."""
    cost = estimate_cost(
        service="unknown_service",
        model="unknown_model",
        max_tokens=1000
    )
    
    assert cost == 0.01, "Should return default $0.01 for unknown model"
```

## Integration Tests

### Test 4: End-to-End Request Flow

```python
# tests/test_e2e_usage_limits.py

import pytest
import json
from httpx import AsyncClient
from src.main import app

@pytest.mark.asyncio
async def test_request_rejected_at_limit():
    """Test that request is rejected when at limit."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        # Set up: bridge at $100 limit
        # (Assume MongoDB is seeded with this limit)
        
        response = await client.post(
            "/api/chat",
            headers={
                "X-Org-ID": "test_org",
                "X-Bridge-ID": "test_bridge",
                "X-Estimated-Cost": "0.01"
            },
            json={"prompt": "Hello"}
        )
        
        # Should be rejected with 429
        assert response.status_code == 429
        data = response.json()
        assert "limit exceeded" in data["error"].lower()

@pytest.mark.asyncio
async def test_request_accepted_under_limit():
    """Test that request is accepted when under limit."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/api/chat",
            headers={
                "X-Org-ID": "test_org",
                "X-Bridge-ID": "test_bridge",
                "X-Estimated-Cost": "0.01"
            },
            json={"prompt": "Hello"}
        )
        
        # Should be accepted
        assert response.status_code == 200

@pytest.mark.asyncio
async def test_usage_event_published_to_rabbitmq():
    """Test that usage event is published to RabbitMQ."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/api/chat",
            headers={
                "X-Org-ID": "test_org",
                "X-Bridge-ID": "test_bridge",
                "X-Estimated-Cost": "0.01"
            },
            json={"prompt": "Hello"}
        )
        
        # Check RabbitMQ queue
        # (In real test, you'd connect to RabbitMQ and verify message)
        assert response.status_code == 200
```

### Test 5: Consumer Processing

```python
# tests/test_usage_events_consumer.py

import pytest
import json
from src.consumers.usageEventsConsumer import UsageEventsBatcher

@pytest.mark.asyncio
async def test_batcher_flushes_at_batch_size():
    """Test that batcher flushes when batch size is reached."""
    batcher = UsageEventsBatcher()
    
    # Mock flush to track calls
    flush_called = False
    original_flush = batcher.flush
    
    async def mock_flush(trigger):
        nonlocal flush_called
        flush_called = True
        await original_flush(trigger)
    
    batcher.flush = mock_flush
    
    # Add 1000 events
    for i in range(1000):
        await batcher.process(
            type('Message', (), {
                'content': json.dumps({
                    "request_id": f"req_{i}",
                    "org_id": "test_org",
                    "bridge_id": "test_bridge",
                    "cost_usd": 0.01
                }).encode()
            })(),
            type('Channel', (), {'ack': lambda x: None, 'nack': lambda x, y, z: None})()
        )
    
    # Should have triggered flush at batch size
    assert flush_called, "Flush should be called at batch size"

@pytest.mark.asyncio
async def test_batcher_flushes_on_timer():
    """Test that batcher flushes after timer interval."""
    batcher = UsageEventsBatcher()
    
    # Add one event
    await batcher.process(
        type('Message', (), {
            'content': json.dumps({
                "request_id": "req_1",
                "org_id": "test_org",
                "bridge_id": "test_bridge",
                "cost_usd": 0.01
            }).encode()
        })(),
        type('Channel', (), {'ack': lambda x: None})()
    )
    
    # Wait for timer (1 second)
    await asyncio.sleep(1.5)
    
    # Should have flushed
    assert len(batcher.buffer) == 0, "Buffer should be empty after flush"
```

### Test 6: Reconciliation

```python
# tests/test_reconciliation.py

import pytest
from src.services.usage_reconciliation_service import reconcile_usage

@pytest.mark.asyncio
async def test_reconciliation_corrects_large_drift():
    """Test that reconciliation corrects large differences."""
    # Set Redis to $100, TimescaleDB to $95
    # Run reconciliation
    # Verify Redis corrected to $95
    
    # (This would require setting up both Redis and TimescaleDB)
    pass

@pytest.mark.asyncio
async def test_reconciliation_ignores_small_drift():
    """Test that reconciliation ignores small differences."""
    # Set Redis to $100, TimescaleDB to $99.50
    # Run reconciliation
    # Verify Redis unchanged (difference < 10%)
    
    pass
```

## Manual Testing

### Scenario 1: Request Rejected at Limit

```bash
# 1. Set up MongoDB limit
mongosh << 'EOF'
use gtwy_db
db.bridges.insertOne({
  _id: "test_bridge_1",
  org_id: "test_org_1",
  bridge_limit: 0.10,
  bridge_reset_period: "monthly",
  bridge_start_date: new Date(),
  bridge_hard_stop: true,
  folder_limit: 0,
  folder_reset_period: "monthly",
  folder_start_date: new Date(),
  folder_hard_stop: true,
  apikey_limit: 0,
  apikey_reset_period: "monthly",
  apikey_start_date: new Date(),
  apikey_hard_stop: true
})
EOF

# 2. Set Redis counter to $0.09
redis-cli SET "AIMIDDLEWARE_dev_quota:test_org_1:bridge:test_bridge_1:monthly:2024-01-15T00:00:00" "0.09"

# 3. Send request with $0.05 estimate (would exceed $0.10 limit)
curl -X POST http://localhost:8000/api/chat \
  -H "X-Org-ID: test_org_1" \
  -H "X-Bridge-ID: test_bridge_1" \
  -H "X-Estimated-Cost: 0.05" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Hello"}'

# Expected: 429 Too Many Requests
# {
#   "error": "Bridge limit exceeded",
#   "limit_type": "bridge",
#   "current_usage": 0.09,
#   "limit_value": 0.10
# }
```

### Scenario 2: Request Accepted and Usage Event Published

```bash
# 1. Set Redis counter to $0.05
redis-cli SET "AIMIDDLEWARE_dev_quota:test_org_1:bridge:test_bridge_1:monthly:2024-01-15T00:00:00" "0.05"

# 2. Send request with $0.03 estimate (within limit)
curl -X POST http://localhost:8000/api/chat \
  -H "X-Org-ID: test_org_1" \
  -H "X-Bridge-ID: test_bridge_1" \
  -H "X-Estimated-Cost: 0.03" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Hello"}'

# Expected: 200 OK with response

# 3. Verify Redis counter was incremented
redis-cli GET "AIMIDDLEWARE_dev_quota:test_org_1:bridge:test_bridge_1:monthly:2024-01-15T00:00:00"
# Expected: 0.08 (0.05 + 0.03)

# 4. Check RabbitMQ queue
rabbitmqctl list_queues usage_events
# Expected: usage_events with 1 message

# 5. Verify message in queue
# (Use RabbitMQ management UI or amqp-cli)
```

### Scenario 3: Consumer Writes to TimescaleDB

```bash
# 1. Start consumer (if not already running)
node src/consumers/index.js

# 2. Wait for batch to flush (1 second)
sleep 2

# 3. Query TimescaleDB
psql -h localhost -U postgres -d timescale_db << 'EOF'
SELECT request_id, org_id, bridge_id, cost_usd, timestamp
FROM usage_events
WHERE org_id = 'test_org_1'
ORDER BY timestamp DESC
LIMIT 10;
EOF

# Expected: 1 row with the usage event
```

### Scenario 4: Reconciliation Corrects Drift

```bash
# 1. Set Redis to $1.00
redis-cli SET "AIMIDDLEWARE_dev_quota:test_org_1:bridge:test_bridge_1:monthly:2024-01-15T00:00:00" "1.00"

# 2. Verify TimescaleDB has $0.90
psql -h localhost -U postgres -d timescale_db << 'EOF'
SELECT SUM(cost_usd) as total
FROM usage_events
WHERE org_id = 'test_org_1'
  AND bridge_id = 'test_bridge_1'
  AND timestamp >= '2024-01-15'::date;
EOF

# 3. Run reconciliation
python << 'EOF'
import asyncio
from src.services.usage_reconciliation_service import reconcile_usage
from datetime import datetime

asyncio.run(reconcile_usage(
    org_id="test_org_1",
    bridge_id="test_bridge_1",
    entity_type="bridge",
    period="monthly",
    start_date=datetime(2024, 1, 15)
))
EOF

# 4. Verify Redis was corrected to $0.90
redis-cli GET "AIMIDDLEWARE_dev_quota:test_org_1:bridge:test_bridge_1:monthly:2024-01-15T00:00:00"
# Expected: 0.9
```

## Performance Testing

### Load Test: 1000 Concurrent Requests

```bash
# Using Apache Bench
ab -n 1000 -c 100 \
  -H "X-Org-ID: test_org" \
  -H "X-Bridge-ID: test_bridge" \
  -H "X-Estimated-Cost: 0.01" \
  http://localhost:8000/api/chat

# Expected:
# - All requests complete in <5 seconds
# - No 5xx errors
# - Lua script execution <1ms per request
```

### Throughput Test: Consumer Batch Processing

```bash
# Publish 10,000 events to RabbitMQ
python << 'EOF'
import asyncio
import json
from src.services.usage_events_producer import initialize_producer, publish_usage_event

async def test():
    await initialize_producer()
    for i in range(10000):
        await publish_usage_event(
            request_id=f"req_{i}",
            org_id="test_org",
            bridge_id="test_bridge",
            service="openai",
            model="gpt-4",
            tokens_in=100,
            tokens_out=50,
            cost_usd=0.01
        )
    print("Published 10,000 events")

asyncio.run(test())
EOF

# Measure time to process all events
time node src/consumers/index.js

# Expected: All 10,000 events processed in <30 seconds
# (1000 events per batch × 10 batches, 1 second per batch)
```

## Debugging

### Enable Debug Logging

```python
# In your main app
import logging
logging.basicConfig(level=logging.DEBUG)

# Or set environment variable
export LOG_LEVEL=DEBUG
```

### Check Redis Keys

```bash
# List all quota keys
redis-cli KEYS "AIMIDDLEWARE_*quota*"

# Get value of specific key
redis-cli GET "AIMIDDLEWARE_dev_quota:test_org:bridge:test_bridge:monthly:2024-01-15T00:00:00"

# Check TTL
redis-cli TTL "AIMIDDLEWARE_dev_quota:test_org:bridge:test_bridge:monthly:2024-01-15T00:00:00"

# Clear all quota keys
redis-cli DEL $(redis-cli KEYS "AIMIDDLEWARE_*quota*")
```

### Check RabbitMQ Queue

```bash
# List all queues
rabbitmqctl list_queues

# Get queue details
rabbitmqctl list_queue_details usage_events

# Purge queue (delete all messages)
rabbitmqctl purge_queue usage_events

# Monitor queue in real-time
watch -n 1 'rabbitmqctl list_queues usage_events'
```

### Check TimescaleDB

```bash
# Count usage events
psql -h localhost -U postgres -d timescale_db -c "SELECT COUNT(*) FROM usage_events;"

# Check for duplicates (should be 0)
psql -h localhost -U postgres -d timescale_db -c "SELECT request_id, COUNT(*) FROM usage_events GROUP BY request_id HAVING COUNT(*) > 1;"

# View recent events
psql -h localhost -U postgres -d timescale_db -c "SELECT * FROM usage_events ORDER BY timestamp DESC LIMIT 20;"

# Check materialized view
psql -h localhost -U postgres -d timescale_db -c "SELECT * FROM daily_usage_summary ORDER BY date DESC LIMIT 10;"
```

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Usage Limits Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    
    services:
      redis:
        image: redis:7
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
        ports:
          - 6379:6379
      
      postgres:
        image: timescale/timescaledb:latest-pg15
        env:
          POSTGRES_PASSWORD: postgres
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
        ports:
          - 5432:5432
      
      rabbitmq:
        image: rabbitmq:3.12
        options: >-
          --health-cmd "rabbitmq-diagnostics ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
        ports:
          - 5672:5672
      
      mongodb:
        image: mongo:7
        ports:
          - 27017:27017
    
    steps:
      - uses: actions/checkout@v3
      
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      
      - name: Install dependencies
        run: |
          pip install -r req.txt
          pip install pytest pytest-asyncio
      
      - name: Run tests
        run: pytest tests/ -v
```

