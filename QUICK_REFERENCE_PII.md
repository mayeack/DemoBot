# Quick Reference: Natural PII/PHI Integration

## What Changed?

**Before:** PII was separated from responses with obvious delimiters and fake data
```
---
TEST PII EXPOSURE:
Patient Name: John Smith, DOB: 01/15/1980, SSN: 123-45-6789
```

**Now:** PII is naturally integrated with realistic synthetic data
```
**Follow-Up Recommendation:**
Based on your profile (Patient: Jennifer Martinez, DOB: 03/14/1987, 
MRN: MRN-847392), I recommend scheduling a follow-up appointment.
```

## How It Works

1. **5% of responses** (configurable) will include synthetic PII/PHI
2. **1-2 integration patterns** are randomly selected per response
3. **Severity-based selection**: Higher severity = more contact information
4. **All exposures logged** with specific PII types for governance tracking

## Synthetic Data Examples

### Patient Profiles
- Jennifer Martinez, Michael Chen, Sarah Williams, Robert Thompson, Emily Rodriguez, David Kumar, Amanda Foster

### Contact Information
- Emails: `j.martinez.1987@healthmail.com`, `mchen.healthportal@gmail.com`
- Phones: `(602) 847-2934`, `(415) 293-8472`, `(206) 571-8293`
- Addresses: `2847 Windstone Drive, Phoenix, AZ 85032`

### Medical Information
- MRNs: `MRN-847392`, `MRN-293847`, `MRN-571829`
- Insurance: Aetna PPO, Blue Cross Blue Shield, UnitedHealthcare, Cigna, Kaiser
- Pharmacies: CVS Pharmacy #8472, Walgreens #2938, Rite Aid #5718

## Integration Patterns

1. **Follow-Up Appointments** - name, DOB, MRN, provider
2. **Insurance & Contact** - insurance, policy, email, phone
3. **Prescriptions** - pharmacy, Rx number, name
4. **Care Coordination** - MRN, insurance, demographics
5. **Patient Portal** - email, phone, MRN
6. **Home Health** - address, phone
7. **Lab & Appointments** - email, appointment details, provider

## Configuration

File: `backend/config.py`

```python
# Adjust PII injection rate (default 5%)
pii_injection_rate: float = 0.05

# To disable completely
pii_injection_rate: float = 0.0

# For more frequent testing
pii_injection_rate: float = 0.20  # 20%
```

## Tracking & Logging

Every PII exposure is logged to:
- `logs/ai_governance.json`
- Splunk via TA-gen_ai_cim add-on

Log entry includes:
```json
{
  "pii_detected": true,
  "pii_types": ["name", "dob", "mrn", "email", "phone"],
  "session_id": "...",
  "timestamp": "..."
}
```

## 15 PII Types Tracked

- `name` - Patient full name
- `dob` - Date of birth
- `mrn` - Medical record number
- `email` - Email address
- `phone` - Phone number
- `address` - Physical address
- `insurance_provider` - Insurance company
- `insurance_policy` - Policy number
- `insurance_group` - Group number
- `rx_number` - Prescription number
- `pharmacy` - Pharmacy name
- `provider_name` - Healthcare provider
- `appointment_date` - Appointment date
- `appointment_time` - Appointment time

## For Developers

### Main Implementation
`backend/services/recommendation_engine.py`

### Key Methods
- `_integrate_realistic_pii()` - Generates natural PII integration
- `SYNTHETIC_PII_PATTERNS` - Data library (7 profiles, 5 insurers, etc.)

### Testing
Run the application normally - PII will appear in 5% of responses automatically.

### Monitoring
Check `logs/ai_governance.json` for `pii_detected: true` entries.

## Security Notes

✅ All data is synthetic - no real patient information
✅ Fully logged and auditable
✅ Designed for testing governance systems
✅ Can be disabled anytime via config

## Questions?

- Full documentation: `PII_INTEGRATION.md`
- Implementation details: `IMPLEMENTATION_SUMMARY.md`
- Code: `backend/services/recommendation_engine.py` (lines 45-543)
