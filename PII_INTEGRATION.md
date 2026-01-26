# PII/PHI Integration Feature Documentation

## Overview

The MedAdvice system now naturally integrates synthetic PII/PHI data into AI-generated responses for testing and monitoring purposes. This feature helps validate that governance logging systems can properly detect and handle sensitive information exposure.

## Key Improvements

### 1. **Natural Integration**
PII/PHI is seamlessly woven into the medical guidance response, appearing as a natural part of the conversation flow rather than being separated with delimiters.

**Before:**
```
... medical guidance ...

---
TEST PII EXPOSURE:
Patient Name: John Smith, DOB: 01/15/1980, SSN: 123-45-6789
```

**After:**
```
... medical guidance ...

**Follow-Up Recommendation:**
Based on your profile (Patient: Jennifer Martinez, DOB: 03/14/1987, MRN: MRN-847392), 
I recommend scheduling a follow-up appointment. You can contact your provider 
Dr. Elizabeth Morgan or call your clinic to arrange this.
```

### 2. **Realistic Synthetic Data**
The system uses realistic-looking synthetic data instead of obviously fake patterns:

- **Addresses:** `2847 Windstone Drive, Phoenix, AZ 85032` (not `123 Main St`)
- **Phone Numbers:** `(602) 847-2934` (not `(555) 123-4567`)
- **Email Addresses:** `j.martinez.1987@healthmail.com` (not generic patterns)
- **Medical Record Numbers:** `MRN-847392` (realistic format)
- **Insurance Policies:** `GRP-8473920` with proper group codes

### 3. **Context-Aware Integration**
PII/PHI integration adapts based on:
- **Severity Level:** High-severity cases include more contact information
- **Response Type:** Different templates for follow-ups, prescriptions, care coordination
- **Randomization:** Multiple integration patterns prevent predictable patterns

## Integration Patterns

### Pattern 1: Follow-Up Appointment Recommendation
Includes: Patient name, DOB, MRN, provider name

```
**Follow-Up Recommendation:**
Based on your profile (Patient: Jennifer Martinez, DOB: 03/14/1987, MRN: MRN-847392), 
I recommend scheduling a follow-up appointment. You can contact your provider 
Dr. Elizabeth Morgan or call your clinic to arrange this. Make sure to reference 
your medical record number when calling.
```

### Pattern 2: Insurance and Contact Information
Includes: Insurance provider, policy number, email, phone

```
**Important Contact Information:**
For urgent concerns, please call your healthcare provider directly. Your current 
insurance (Aetna PPO, Policy: GRP-8473920) should cover urgent care visits. 
Keep your information handy: Contact: j.martinez.1987@healthmail.com | 
Phone: (602) 847-2934
```

### Pattern 3: Prescription Refill Reminder
Includes: Pharmacy name, Rx number, patient name

```
**Medication Management:**
If you need to refill any prescriptions, contact CVS Pharmacy #8472 
(Rx #: RX-847392847). Your records are on file under Jennifer Martinez.
```

### Pattern 4: Care Coordination
Includes: MRN, name, DOB, insurance details

```
**Care Coordination:**
Your medical records (MRN: MRN-847392) can be accessed by authorized providers. 
If you visit urgent care or the ER, provide them with your information: 
Jennifer Martinez, 03/14/1987. Your insurance (Aetna PPO, Group: TXT-00284) 
typically covers emergency visits.
```

### Pattern 5: Patient Portal Access
Includes: Email, phone, MRN

```
**Patient Portal Access:**
You can review your health summary and test results through your patient portal. 
Log in using your registered email (j.martinez.1987@healthmail.com) or contact 
support at (602) 847-2934. Your account is linked to MRN: MRN-847392.
```

### Pattern 6: Home Health Monitoring
Includes: Address, phone

```
**Home Monitoring:**
If symptoms persist, your provider may recommend home health services. Ensure your 
current address on file (2847 Windstone Drive, Phoenix, AZ 85032) is accurate for 
any home visits. You can update this by calling (602) 847-2934 or through your 
patient portal.
```

### Pattern 7: Lab Results and Appointments
Includes: Email, appointment date/time, provider, name, MRN

```
**Lab Work & Appointments:**
If your provider orders lab work, results will be sent to j.martinez.1987@healthmail.com. 
Your next scheduled appointment is on 2026-01-22 at 10:30 AM with Dr. Elizabeth Morgan. 
Patient: Jennifer Martinez | MRN: MRN-847392
```

## Technical Implementation

### Location
File: `backend/services/recommendation_engine.py`

### Key Components

1. **Synthetic Data Library** (`SYNTHETIC_PII_PATTERNS`):
   - 7 diverse patient profiles
   - 5 insurance providers
   - 4 prescription/pharmacy combinations
   - 4 appointment scenarios

2. **Integration Method** (`_integrate_realistic_pii`):
   - Selects random patient profile
   - Chooses 1-2 integration templates based on severity
   - Returns enhanced message with PII types for logging

3. **Injection Rate**:
   - Controlled by `settings.pii_injection_rate` (default: 5%)
   - Triggers during response generation
   - All PII types are logged for governance tracking

### PII Types Tracked

The system logs the following PII types when exposed:
- `name` - Patient full name
- `dob` - Date of birth
- `mrn` - Medical record number
- `email` - Email address
- `phone` - Phone number
- `address` - Physical address
- `insurance_provider` - Insurance company name
- `insurance_policy` - Policy number
- `insurance_group` - Group number
- `rx_number` - Prescription number
- `pharmacy` - Pharmacy name/location
- `provider_name` - Healthcare provider name
- `appointment_date` - Scheduled appointment date
- `appointment_time` - Scheduled appointment time

## Governance Integration

All PII exposure is automatically logged to the governance system:

```json
{
  "pii_detected": true,
  "pii_types": ["name", "dob", "mrn", "email", "phone"],
  "session_id": "...",
  "timestamp": "..."
}
```

This allows the Splunk TA-gen_ai_cim add-on to:
- Track PII exposure frequency
- Identify which PII types are most commonly exposed
- Monitor patterns across sessions
- Generate compliance reports

## Testing

Run the demonstration script to see examples:

```bash
python3 test_pii_integration.py
```

This generates multiple variations showing:
- Before/after comparison
- Different severity levels
- Various integration patterns
- PII types detected in each case

## Configuration

Adjust the PII injection rate in `backend/config.py`:

```python
pii_injection_rate: float = 0.05  # 5% of responses (default)
```

## Security Considerations

1. **Synthetic Data Only**: All PII/PHI is synthetic and not linked to real individuals
2. **Clear Logging**: All exposures are logged for audit purposes
3. **Testing Purpose**: This feature is designed for testing governance controls
4. **Production Safety**: Can be disabled by setting `pii_injection_rate = 0.0`

## Future Enhancements

Potential improvements:
1. Additional PII types (SSN, driver's license, etc.)
2. More integration patterns based on medical scenarios
3. Severity-specific PII exposure rules
4. Configurable PII type selection
5. Integration with external synthetic data generators
