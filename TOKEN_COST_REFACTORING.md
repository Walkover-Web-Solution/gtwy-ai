# Token Cost Calculator - Refactoring Summary

## What Changed

Refactored `src/utils/token_cost_calculator.py` to use dynamic pricing from `model_config_document` instead of hardcoded pricing configuration.

## Before (Hardcoded)

```python
PRICING_CONFIG = {
    "openai": {
        "gpt-4": {"input": 0.03, "output": 0.06},
        "gpt-4-turbo": {"input": 0.01, "output": 0.03},
        # ... more hardcoded prices
    },
    "anthropic": {
        "claude-3-opus": {"input": 0.015, "output": 0.075},
        # ... more hardcoded prices
    },
    # ... more services
}

def estimate_cost(service: str, model: str, max_tokens: int, input_tokens: Optional[int] = None) -> float:
    service_pricing = PRICING_CONFIG.get(service.lower(), {})
    model_pricing = service_pricing.get(model.lower())
    # ... rest of logic
```

**Issues**:
- ❌ Hardcoded pricing requires code changes to update
- ❌ Deployment needed for any pricing changes
- ❌ Not synchronized with MongoDB model configuration
- ❌ Duplicate pricing information

## After (Dynamic from MongoDB)

```python
from src.configs.model_configuration import model_config_document

def estimate_cost(service: str, model: str, max_tokens: int, input_tokens: Optional[int] = None) -> float:
    service_config = model_config_document.get(service.lower(), {})
    model_config = service_config.get(model_lower)
    
    output_config = model_config.get("outputConfig", {})
    usage_config = output_config.get("usage", [{}])[0]
    
    input_price = float(usage_config.get("inputPrice", 0))
    output_price = float(usage_config.get("outputPrice", 0))
    # ... rest of logic
```

**Benefits**:
- ✅ Pricing fetched from MongoDB `modelconfigurations` collection
- ✅ No code changes needed for pricing updates
- ✅ Automatic sync with model configuration
- ✅ Single source of truth (MongoDB)
- ✅ Real-time updates via change stream listener

## How It Works

### 1. Model Configuration Document Structure

MongoDB `modelconfigurations` collection:
```javascript
{
  service: "openai",
  model_name: "gpt-4",
  status: 1,
  outputConfig: {
    usage: [
      {
        inputPrice: 0.03,    // Per 1000 tokens
        outputPrice: 0.06    // Per 1000 tokens
      }
    ]
  }
}
```

### 2. Loading Configuration

On startup, `src/configs/model_configuration.py`:
```python
async def init_model_configuration():
    new_document = await get_model_configurations()
    model_config_document.update(new_document)
```

Structure after loading:
```python
model_config_document = {
    "openai": {
        "gpt-4": { ... model config ... },
        "gpt-4-turbo": { ... model config ... },
        "gpt-3.5-turbo": { ... model config ... }
    },
    "anthropic": {
        "claude-3-opus": { ... model config ... },
        "claude-3-sonnet": { ... model config ... }
    },
    # ... more services
}
```

### 3. Real-Time Updates

Change stream listener automatically updates `model_config_document` when MongoDB changes:
```python
async def _async_change_listener():
    async with model_config_model.watch(pipeline) as stream:
        async for change in stream:
            await init_model_configuration()  # Refresh on any change
```

### 4. Cost Calculation

Both `estimate_cost()` and `calculate_actual_cost()` now:
1. Look up service in `model_config_document`
2. Get model configuration
3. Extract `inputPrice` and `outputPrice` from `outputConfig.usage[0]`
4. Calculate cost: `(tokens * price / 1000)`

## API Compatibility

**No changes to function signatures**:
```python
# Same API as before
estimate_cost(service="openai", model="gpt-4", max_tokens=2000, input_tokens=100)
calculate_actual_cost(service="openai", model="gpt-4", tokens_in=150, tokens_out=500)
```

**Same return type**: `float` (cost in USD)

**Same error handling**: Returns `0.01` default if model not found

## Migration Path

### Step 1: Deploy Code
Deploy the refactored `token_cost_calculator.py` with the new logic.

### Step 2: Verify MongoDB Configuration
Ensure all models in `modelconfigurations` collection have:
- `service` field (e.g., "openai")
- `model_name` field (e.g., "gpt-4")
- `outputConfig.usage[0].inputPrice` field
- `outputConfig.usage[0].outputPrice` field

### Step 3: Test
```python
# Test that pricing is fetched correctly
from src.utils.token_cost_calculator import estimate_cost

cost = estimate_cost(service="openai", model="gpt-4", max_tokens=2000, input_tokens=100)
print(f"Estimated cost: ${cost}")  # Should match MongoDB pricing
```

### Step 4: Monitor
- Check logs for warnings about missing models
- Verify pricing calculations match expectations
- Monitor for any pricing discrepancies

## Fallback Behavior

If a model is not found in `model_config_document`:
1. Log warning: `"No pricing found for {service}/{model}, using default $0.01"`
2. Return default cost: `0.01`
3. Request is still processed (doesn't block)

This ensures graceful degradation if pricing is missing.

## Files Modified

| File | Changes |
|------|---------|
| `src/utils/token_cost_calculator.py` | Removed hardcoded PRICING_CONFIG, added import of model_config_document, refactored both functions to fetch pricing dynamically |
| `USAGE_LIMITS_INTEGRATION.md` | Added "Pricing Configuration" section explaining dynamic pricing |

## Benefits Summary

| Aspect | Before | After |
|--------|--------|-------|
| Pricing Source | Hardcoded in code | MongoDB collection |
| Update Method | Code deployment | MongoDB update |
| Sync | Manual | Automatic (change stream) |
| Maintenance | High | Low |
| Flexibility | Low | High |
| Single Source of Truth | No | Yes |

## Testing

### Unit Test Example

```python
async def test_estimate_cost_uses_model_config():
    """Verify pricing is fetched from model_config_document."""
    # Mock model_config_document
    model_config_document["openai"]["gpt-4"] = {
        "outputConfig": {
            "usage": [
                {"inputPrice": 0.05, "outputPrice": 0.10}
            ]
        }
    }
    
    cost = estimate_cost(
        service="openai",
        model="gpt-4",
        max_tokens=1000,
        input_tokens=100
    )
    
    # Expected: (100 * 0.05 / 1000) + (1000 * 0.10 / 1000) = 0.1005
    assert cost == pytest.approx(0.1005, rel=0.01)
```

### Integration Test Example

```python
async def test_cost_calculation_with_real_model_config():
    """Test with actual MongoDB model configuration."""
    # Ensure model exists in MongoDB
    await db["modelconfigurations"].insert_one({
        "service": "openai",
        "model_name": "test-model",
        "status": 1,
        "outputConfig": {
            "usage": [
                {"inputPrice": 0.001, "outputPrice": 0.002}
            ]
        }
    })
    
    # Reload configuration
    await init_model_configuration()
    
    # Test cost calculation
    cost = estimate_cost(
        service="openai",
        model="test-model",
        max_tokens=1000,
        input_tokens=500
    )
    
    # Expected: (500 * 0.001 / 1000) + (1000 * 0.002 / 1000) = 0.0025
    assert cost == pytest.approx(0.0025, rel=0.01)
```

## Conclusion

The refactoring successfully decouples pricing from code, making the system more flexible and maintainable. Pricing is now managed through MongoDB, allowing real-time updates without code deployment.

---

**Date**: May 14, 2026
**Status**: Complete
**Breaking Changes**: None
**Migration Required**: No (backward compatible)
