"""
Redis Lua scripts for atomic usage limit checking and reservation.
These scripts run atomically inside Redis, preventing race conditions.
"""

CHECK_AND_RESERVE_SCRIPT = """
-- KEYS: [bridge_key, folder_key, apikey_key]
-- ARGV: [bridge_limit, folder_limit, apikey_limit, reservation_amount]
-- Returns: 1 if all checks pass and reservation is made, 0 if any limit exceeded

local bridge_key = KEYS[1]
local folder_key = KEYS[2]
local apikey_key = KEYS[3]

local bridge_limit = tonumber(ARGV[1])
local folder_limit = tonumber(ARGV[2])
local apikey_limit = tonumber(ARGV[3])
local reservation_amount = tonumber(ARGV[4])

-- Get current usage values (default to 0 if key doesn't exist)
local bridge_usage = tonumber(redis.call('GET', bridge_key) or 0)
local folder_usage = tonumber(redis.call('GET', folder_key) or 0)
local apikey_usage = tonumber(redis.call('GET', apikey_key) or 0)

-- Check if any limit would be exceeded
if bridge_limit > 0 and (bridge_usage + reservation_amount) > bridge_limit then
    return {0, "bridge", bridge_usage, bridge_limit}
end

if folder_limit > 0 and (folder_usage + reservation_amount) > folder_limit then
    return {0, "folder", folder_usage, folder_limit}
end

if apikey_limit > 0 and (apikey_usage + reservation_amount) > apikey_limit then
    return {0, "apikey", apikey_usage, apikey_limit}
end

-- All checks passed, make the reservation
if bridge_limit > 0 then
    redis.call('INCRBYFLOAT', bridge_key, reservation_amount)
end

if folder_limit > 0 then
    redis.call('INCRBYFLOAT', folder_key, reservation_amount)
end

if apikey_limit > 0 then
    redis.call('INCRBYFLOAT', apikey_key, reservation_amount)
end

return {1}
"""

SETTLE_DIFFERENCE_SCRIPT = """
-- KEYS: [bridge_key, folder_key, apikey_key]
-- ARGV: [bridge_adjustment, folder_adjustment, apikey_adjustment]
-- Adjusts usage by the difference between reservation and actual cost

local bridge_key = KEYS[1]
local folder_key = KEYS[2]
local apikey_key = KEYS[3]

local bridge_adjustment = tonumber(ARGV[1])
local folder_adjustment = tonumber(ARGV[2])
local apikey_adjustment = tonumber(ARGV[3])

-- Apply adjustments (usually negative to give back unused reservation)
if bridge_adjustment ~= 0 then
    redis.call('INCRBYFLOAT', bridge_key, bridge_adjustment)
end

if folder_adjustment ~= 0 then
    redis.call('INCRBYFLOAT', folder_key, folder_adjustment)
end

if apikey_adjustment ~= 0 then
    redis.call('INCRBYFLOAT', apikey_key, apikey_adjustment)
end

return 1
"""

GET_USAGE_SCRIPT = """
-- KEYS: [bridge_key, folder_key, apikey_key]
-- Returns: [bridge_usage, folder_usage, apikey_usage]

local bridge_key = KEYS[1]
local folder_key = KEYS[2]
local apikey_key = KEYS[3]

local bridge_usage = tonumber(redis.call('GET', bridge_key) or 0)
local folder_usage = tonumber(redis.call('GET', folder_key) or 0)
local apikey_usage = tonumber(redis.call('GET', apikey_key) or 0)

return {bridge_usage, folder_usage, apikey_usage}
"""
