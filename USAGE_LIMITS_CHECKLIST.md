# Usage Limits Implementation Checklist

## Pre-Implementation

- [ ] Review architecture diagram and understand the flow
- [ ] Ensure all services are running (Redis, RabbitMQ, PostgreSQL/TimescaleDB, MongoDB)
- [ ] Backup existing data
- [ ] Create feature flag for gradual rollout

## Phase 1: Infrastructure Setup

### Python Services
- [ ] Create `src/services/usage_limits_service.py` ✅
- [ ] Create `src/services/usage_events_producer.py` ✅
- [ ] Create `src/utils/token_cost_calculator.py` ✅
- [ ] Create `src/middlewares/usage_limits_middleware.py` ✅
- [ ] Create `src/services/usage_reconciliation_service.py` ✅

### Node.js Services
- [ ] Create `src/consumers/usageEventsConsumer.js` ✅
- [ ] Create `src/services/logQueue/saveUsageEvents.service.js` ✅
- [ ] Update `src/consumers/index.js` to include usage events consumer ✅

### Database
- [ ] Create TimescaleDB migration for `usage_events` table ✅
- [ ] Run migration: `npm run migrateTimescale:up`
- [ ] Verify table created: `psql -c "SELECT * FROM usage_events LIMIT 1;"`
- [ ] Create MongoDB model for limit configuration ✅

### Documentation
- [ ] Create `USAGE_LIMITS_INTEGRATION.md` ✅
- [ ] Create `USAGE_LIMITS_TESTING.md` ✅
- [ ] Create example controller `src/controllers/usage_limits_example.py` ✅

## Phase 2: Configuration

### Environment Variables
- [ ] Add `USAGE_EVENTS_QUEUE_NAME` to `.env`
- [ ] Add `USAGE_EVENTS_FAILED_QUEUE_NAME` to `.env`
- [ ] Verify Redis URI is correct
- [ ] Verify RabbitMQ connection URL is correct
- [ ] Verify TimescaleDB connection details are correct

### MongoDB Setup
- [ ] Create indexes on bridges collection:
  ```javascript
  db.bridges.createIndex({ org_id: 1 })
  db.bridges.createIndex({ _id: 1, org_id: 1 })
  ```
- [ ] Insert test limit configuration:
  ```javascript
  db.bridges.updateOne(
    { _id: "test_bridge" },
    { $set: {
      org_id: "test_org",
      bridge_limit: 100.0,
      bridge_reset_period: "monthly",
      bridge_start_date: new Date(),
      bridge_hard_stop: true,
      folder_limit: 500.0,
      folder_reset_period: "monthly",
      folder_start_date: new Date(),
      folder_hard_stop: false,
      apikey_limit: 1000.0,
      apikey_reset_period: "monthly",
      apikey_start_date: new Date(),
      apikey_hard_stop: true
    }}
  )
  ```

### RabbitMQ Setup
- [ ] Declare exchange: `usage_events` (direct, durable)
- [ ] Declare queue: `usage_events` (durable)
- [ ] Declare queue: `usage_events_failed` (durable)
- [ ] Bind queue to exchange with routing key: `usage_event`

## Phase 3: Python Integration

### Main App Setup
- [ ] Import services in main FastAPI app
- [ ] Add startup event to initialize `usage_limits_service`
- [ ] Add startup event to initialize RabbitMQ producer
- [ ] Add shutdown event to close RabbitMQ connection
- [ ] Add `UsageLimitsMiddleware` to middleware stack

### Request Headers
- [ ] Document required headers: `X-Org-ID`, `X-Bridge-ID`
- [ ] Document optional headers: `X-Folder-ID`, `X-API-Key-ID`, `X-Estimated-Cost`
- [ ] Update API documentation/OpenAPI spec

### LLM Call Integration
- [ ] Wrap LLM calls with usage tracking
- [ ] Calculate estimated cost before call
- [ ] Calculate actual cost after call
- [ ] Call `settle_usage()` to adjust reservation
- [ ] Call `publish_usage_event()` to record event
- [ ] Handle errors and refund reservation if needed

### Error Handling
- [ ] Handle 429 responses (limit exceeded)
- [ ] Handle Redis connection errors
- [ ] Handle RabbitMQ connection errors
- [ ] Log all errors with request_id for debugging

## Phase 4: Node.js Integration

### Consumer Setup
- [ ] Verify `usageEventsConsumer.js` is imported in `src/consumers/index.js`
- [ ] Verify consumer is registered with correct queue name
- [ ] Verify batch size is 1000
- [ ] Verify flush interval is 1 second

### Database Service
- [ ] Verify `saveUsageEvents.service.js` connects to TimescaleDB
- [ ] Verify batch insert uses COPY command
- [ ] Verify ON CONFLICT DO NOTHING for idempotency
- [ ] Verify failed events are published to failed queue

### Testing
- [ ] Start consumer: `node src/consumers/index.js`
- [ ] Publish test event to RabbitMQ
- [ ] Verify event appears in TimescaleDB
- [ ] Verify consumer acknowledges message

## Phase 5: Testing

### Unit Tests
- [ ] Test Lua script atomicity
- [ ] Test settlement logic
- [ ] Test token cost calculation
- [ ] Test reconciliation logic

### Integration Tests
- [ ] Test request rejected at limit
- [ ] Test request accepted under limit
- [ ] Test usage event published to RabbitMQ
- [ ] Test consumer writes to TimescaleDB
- [ ] Test reconciliation corrects drift

### Manual Tests
- [ ] Test scenario 1: Request rejected at limit
- [ ] Test scenario 2: Request accepted and event published
- [ ] Test scenario 3: Consumer writes to TimescaleDB
- [ ] Test scenario 4: Reconciliation corrects drift

### Load Tests
- [ ] Run 1000 concurrent requests
- [ ] Verify all complete in <5 seconds
- [ ] Verify no 5xx errors
- [ ] Verify Lua script execution <1ms per request

## Phase 6: Monitoring & Observability

### Logging
- [ ] Enable debug logging for usage limits service
- [ ] Enable debug logging for consumer
- [ ] Enable debug logging for reconciliation job

### Metrics
- [ ] Track requests accepted vs rejected
- [ ] Track usage per org/bridge/folder/apikey
- [ ] Track consumer batch processing time
- [ ] Track reconciliation job execution time

### Alerts
- [ ] Alert on high error rate (>1%)
- [ ] Alert on consumer lag (>1000 messages)
- [ ] Alert on large Redis/TimescaleDB drift (>10%)
- [ ] Alert on reconciliation job failures

### Dashboards
- [ ] Create dashboard for usage by org
- [ ] Create dashboard for usage by bridge
- [ ] Create dashboard for usage by service/model
- [ ] Create dashboard for limit status (% of limit used)

## Phase 7: Rollout

### Feature Flag Setup
- [ ] Create feature flag: `usage_limits_enabled`
- [ ] Default to false (disabled)
- [ ] Add flag check in middleware

### Gradual Rollout
- [ ] Enable for 10% of orgs
- [ ] Monitor error rates for 24 hours
- [ ] Enable for 50% of orgs
- [ ] Monitor error rates for 24 hours
- [ ] Enable for 100% of orgs

### Monitoring During Rollout
- [ ] Check error rates
- [ ] Check latency impact
- [ ] Check Redis memory usage
- [ ] Check RabbitMQ queue depth
- [ ] Check TimescaleDB write performance

## Phase 8: Post-Rollout

### Cleanup
- [ ] Remove feature flag (or keep for emergency disable)
- [ ] Remove old usage fields from MongoDB (if any)
- [ ] Archive old usage data (if needed)

### Documentation
- [ ] Update API documentation
- [ ] Update runbooks for operations
- [ ] Update troubleshooting guide
- [ ] Create knowledge base articles

### Training
- [ ] Train support team on new system
- [ ] Train operations team on monitoring
- [ ] Train developers on integration

## Troubleshooting Checklist

### Issue: Requests getting 429 when should be allowed
- [ ] Check MongoDB limit configuration
- [ ] Check Redis counter value
- [ ] Check if limit period has reset
- [ ] Run reconciliation to sync Redis with TimescaleDB
- [ ] Check logs for errors

### Issue: Usage events not appearing in TimescaleDB
- [ ] Check RabbitMQ queue has messages
- [ ] Check consumer is running
- [ ] Check consumer logs for errors
- [ ] Check TimescaleDB connection
- [ ] Check for failed events in failed queue

### Issue: High latency on requests
- [ ] Check Redis latency
- [ ] Check Lua script execution time
- [ ] Check middleware overhead
- [ ] Profile request handler

### Issue: Large drift between Redis and TimescaleDB
- [ ] Check for data loss in RabbitMQ
- [ ] Check for duplicate events in TimescaleDB
- [ ] Check reconciliation job logs
- [ ] Manually correct Redis from TimescaleDB

## Rollback Plan

If issues are discovered:

1. **Disable feature flag** (if using feature flag)
   ```python
   # In middleware
   if not feature_flag_enabled("usage_limits"):
       return await call_next(request)
   ```

2. **Stop consumer** to prevent further writes
   ```bash
   kill $(pgrep -f "node src/consumers/index.js")
   ```

3. **Clear Redis counters** to reset state
   ```bash
   redis-cli DEL $(redis-cli KEYS "AIMIDDLEWARE_*quota*")
   ```

4. **Investigate root cause** using logs and metrics

5. **Fix issue** and test thoroughly

6. **Re-enable** with feature flag

## Sign-Off

- [ ] All tests passing
- [ ] All documentation complete
- [ ] All monitoring in place
- [ ] Rollback plan documented
- [ ] Team trained
- [ ] Ready for production rollout

**Date Completed**: ___________
**Approved By**: ___________
**Notes**: ___________
