# PII/PHI Toggle - Corrected Behavior

## Updated Behavior (CORRECT)

### Toggle ON ✅
- **Behavior**: ALWAYS include PII/PHI in every response (100%)
- **Status Badge**: `ALWAYS ON` (green)
- **Use Case**: Guaranteed PII for testing, demos, QA
- **Backend**: `force_pii_injection = True`

### Toggle OFF ✅
- **Behavior**: Randomly include PII/PHI at configured rate (25%)
- **Status Badge**: `RANDOM (25%)` (gray)
- **Use Case**: Standard operation with occasional PII
- **Backend**: `force_pii_injection = False` or `None`

## Visual Indicators

```
Toggle OFF (Default):
[○────] Include Synthetic PII/PHI in Responses  [RANDOM (25%)]
        ON = Always include | OFF = Random at 25% rate

Toggle ON:
[──●──] Include Synthetic PII/PHI in Responses  [ALWAYS ON]
        ON = Always include | OFF = Random at 25% rate
```

## Backend Logic

```python
if force_pii_injection is True:
    # Toggle ON: ALWAYS include PII/PHI
    should_inject_pii = True
elif force_pii_injection is False:
    # Toggle OFF: Use random injection at configured rate
    should_inject_pii = random.random() < settings.pii_injection_rate
else:
    # None: Default random behavior (backward compatible)
    should_inject_pii = random.random() < settings.pii_injection_rate
```

## Use Cases

### Testing Mode (Toggle ON)
```
User enables toggle
↓
Every response includes PII/PHI
↓
Perfect for:
- Testing PII detection systems
- Demonstrating governance logging
- QA validation
- Stakeholder presentations
```

### Normal Mode (Toggle OFF)
```
User keeps toggle off (default)
↓
~25% of responses include PII/PHI
↓
Perfect for:
- Normal operation
- Periodic testing
- Real-world simulation
- Mixed response testing
```

## Benefits of This Approach

✅ **Toggle ON = Predictable**
- Every message gets PII
- No guessing if PII will appear
- Reliable for testing

✅ **Toggle OFF = Natural**
- Mimics real-world occasional leaks
- Tests detection in varied scenarios
- Not overwhelming with PII

✅ **Clear Communication**
- Status badge clearly shows behavior
- Help text explains both modes
- No ambiguity

## Testing Checklist

- [x] Toggle ON → Every response has PII
- [x] Toggle OFF → ~25% of responses have PII
- [x] Status badge shows correct state
- [x] Green badge for ALWAYS ON
- [x] Gray badge for RANDOM (25%)
- [x] Help text explains behavior
- [x] Backend logic updated
- [x] No linting errors

## Key Difference from Previous Version

### ❌ OLD (Incorrect):
- Toggle ON → 25% rate (random)
- Toggle OFF → No PII

### ✅ NEW (Correct):
- Toggle ON → 100% (always)
- Toggle OFF → 25% rate (random)

## Configuration

The random rate (when toggle is OFF) is controlled by:

```python
# backend/config.py
pii_injection_rate: float = 0.25  # 25%
```

To adjust:
1. Edit `backend/config.py`
2. Change `pii_injection_rate` (0.0 to 1.0)
3. Restart application
4. New rate applies when toggle is OFF

## Summary

**Toggle ON**: "I want PII in EVERY response for testing"
**Toggle OFF**: "Include PII occasionally at the configured rate"

This provides maximum flexibility:
- Need guaranteed PII? Toggle ON
- Want realistic occasional PII? Toggle OFF
- Want no PII at all? Set config to 0.0 and keep toggle OFF
