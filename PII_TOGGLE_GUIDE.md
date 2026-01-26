# PII/PHI Toggle Control - User Guide

## Overview

The MedAdvice v3 chat interface now includes a toggle control that allows users to enable or disable synthetic PII/PHI injection in AI responses. This provides on-demand testing of governance detection systems.

## Feature Location

The toggle is located at the top of the chat interface, directly below the header and above the emergency banner.

## How It Works

### Toggle States

**OFF (Default)**
- Status badge shows: `OFF` (gray)
- PII/PHI injection uses the default random rate (25%)
- Approximately 1 in 4 responses may include synthetic PII/PHI

**ON**
- Status badge shows: `ON (25% Rate)` (blue)
- PII/PHI injection is enabled for all responses at 25% rate
- When enabled via toggle, respects the configured injection rate

### User Interface

```
┌─────────────────────────────────────────────────────────┐
│  [Toggle Switch] Include Synthetic PII/PHI in Responses │
│                  For testing governance detection...     │
│                                           [OFF/ON Badge] │
└─────────────────────────────────────────────────────────┘
```

### Using the Toggle

1. **Enable PII/PHI**: Click the toggle switch to turn it ON
   - The switch will turn blue
   - Status badge changes to "ON (25% Rate)"
   - Future responses in this session will use forced PII injection at the configured rate

2. **Disable PII/PHI**: Click the toggle switch to turn it OFF
   - The switch returns to gray
   - Status badge changes to "OFF"
   - Future responses use default random injection behavior

3. **Persistence**: Your toggle preference is saved in browser localStorage
   - Remains active across page refreshes
   - Persists until you manually change it or clear browser data

## Technical Implementation

### Frontend Changes

**File**: `frontend/index.html`
- Added toggle switch UI component with Tailwind CSS styling
- Displays current PII injection status

**File**: `frontend/js/chat.js`
- `piiEnabled` variable tracks toggle state
- `togglePII()` function handles toggle changes
- `updatePIIStatus()` updates UI status badge
- Toggle state saved to `localStorage` as `medadvice_pii_enabled`
- `force_pii_injection` parameter sent with each message

### Backend Changes

**File**: `backend/models/schemas.py`
- Added optional `force_pii_injection` field to `ChatRequest` model

**File**: `backend/routers/chat.py`
- Passes `force_pii_injection` parameter to recommendation engine

**File**: `backend/services/recommendation_engine.py`
- `process_message()` accepts `force_pii_injection` parameter
- `_generate_recommendation()` accepts `force_pii_injection` parameter
- Logic updated to respect forced PII injection:
  - If `force_pii_injection=True`: Uses configured rate (25%)
  - If `force_pii_injection=False`: Disables PII injection
  - If `force_pii_injection=None`: Uses random injection at configured rate

### Injection Logic

```python
# Determine if PII should be injected
should_inject_pii = False
if force_pii_injection is not None:
    # Explicit override from UI toggle
    should_inject_pii = force_pii_injection
else:
    # Use random injection based on config rate
    should_inject_pii = random.random() < settings.pii_injection_rate

if should_inject_pii:
    final_message, pii_types = self._integrate_realistic_pii(...)
    pii_injected = True
```

## Use Cases

### 1. Testing Governance Systems
Enable the toggle to consistently generate PII-containing responses for testing:
```
1. Enable toggle (ON)
2. Send medical queries
3. Verify PII detection in governance logs
4. Review PII types captured
```

### 2. Demonstrations
Show stakeholders how PII appears in responses:
```
1. Enable toggle (ON)
2. Run through typical medical scenarios
3. Point out naturally integrated PII/PHI
4. Show governance logging of detected PII
```

### 3. Development & QA
Test PII integration patterns during development:
```
1. Enable toggle (ON)
2. Test different severity levels
3. Verify integration templates
4. Validate logging accuracy
```

### 4. Clean Sessions
Disable PII for user demonstrations without governance testing:
```
1. Disable toggle (OFF)
2. Run medical guidance demos
3. Focus on AI recommendations
4. No PII distractions
```

## Configuration

### Injection Rate
Default rate set in `backend/config.py`:
```python
pii_injection_rate: float = 0.25  # 25% of responses
```

When toggle is ON, this rate is applied. When OFF, PII is disabled.

### Adjusting Rate
To change the injection rate:
1. Edit `backend/config.py`
2. Modify `pii_injection_rate` value (0.0 to 1.0)
3. Restart application
4. New rate applies when toggle is ON

## Examples

### Toggle ON - Expected Behavior
```
User: "I have a headache and fever"

Response: [Medical guidance...]

**Follow-Up Recommendation:**
Based on your profile (Patient: Jennifer Martinez, 
DOB: 03/14/1987, MRN: MRN-847392), I recommend 
scheduling a follow-up appointment...
```

### Toggle OFF - Expected Behavior
```
User: "I have a headache and fever"

Response: [Medical guidance only, no PII/PHI]

**Assessment:**
You appear to be experiencing symptoms...
```

## Governance Logging

All PII exposure is logged regardless of how it's triggered:

```json
{
  "pii_detected": true,
  "pii_types": ["name", "dob", "mrn"],
  "session_id": "...",
  "request_id": "...",
  "timestamp": "..."
}
```

Logs can be viewed at:
- **Governance UI**: http://localhost:8001/governance-ui
- **Log File**: `logs/ai_governance.json`

## Browser Compatibility

Toggle uses standard HTML5 checkbox with CSS styling:
- ✅ Chrome/Edge
- ✅ Firefox
- ✅ Safari
- ✅ Mobile browsers

## Troubleshooting

### Toggle Doesn't Appear
- Refresh the page
- Clear browser cache
- Check console for JavaScript errors

### Toggle State Not Persisting
- Check browser localStorage is enabled
- Verify not in incognito/private mode
- Clear localStorage and try again

### PII Not Appearing When Toggle ON
- Check backend logs for errors
- Verify `pii_injection_rate` is not 0.0
- Ensure application has restarted after config changes

### Toggle Shows Wrong State
- Clear localStorage: `localStorage.clear()`
- Refresh page
- Toggle should reset to OFF

## Security Notes

- ✅ All PII/PHI is synthetic (no real patient data)
- ✅ Toggle state is client-side only (localStorage)
- ✅ All exposures are logged for audit
- ✅ Feature designed for testing purposes only

## Future Enhancements

Potential improvements:
1. Rate slider (adjust percentage dynamically)
2. PII type selector (choose which types to include)
3. Session statistics (show PII injection count)
4. Export PII examples for documentation
5. Integration with admin dashboard metrics
