from typing import Dict, Any, List, Optional, Tuple
import uuid
import time
import random
import logging
from datetime import datetime
from anthropic import Anthropic, APIStatusError
from backend.config import settings
from backend.models.schemas import SeverityLevel, MessageType
from backend.logging.governance_logger import governance_logger
from backend.services.escalation_rules import EscalationRules
from backend.services.clarifying_questions import ClarifyingQuestionsService

logger = logging.getLogger(__name__)

class RecommendationEngine:
    """Core medical recommendation engine using Claude"""

    SYSTEM_PROMPT = """You are a medical guidance assistant providing general, non-prescriptive health information.

CRITICAL RULES:
1. You are NOT a replacement for professional medical care
2. NEVER provide prescription drug dosages
3. NEVER provide pediatric dosing for minors without escalation
4. ALWAYS recommend emergency services (911) for life-threatening symptoms
5. Provide general wellness guidance, lifestyle adjustments, and OTC suggestions only
6. Be extremely cautious with vulnerable populations (infants, elderly, pregnant)
7. When in doubt, recommend professional medical evaluation

Your response should include:
1. A preliminary assessment of the situation
2. General guidance (lifestyle, OTC options, home remedies)
3. Clear indicators of when to seek professional care
4. A severity level: LOW, MEDIUM, HIGH, or EMERGENCY

Format your response as JSON:
{
  "assessment": "Brief assessment of the situation",
  "guidance": ["List of general recommendations"],
  "seek_care_if": ["Conditions requiring professional care"],
  "severity": "LOW|MEDIUM|HIGH|EMERGENCY",
  "confidence": 0.0-1.0
}

IMPORTANT: The "severity" field must be EXACTLY one of these four values: LOW, MEDIUM, HIGH, or EMERGENCY.
Do NOT use combined values like "MEDIUM-HIGH" or "LOW-MEDIUM". Choose the single most appropriate level."""

    # Realistic synthetic PII/PHI patterns for natural integration
    SYNTHETIC_PII_PATTERNS = {
        "patient_profiles": [
            {
                "name": "Jennifer Martinez",
                "dob": "03/14/1987",
                "age": 38,
                "mrn": "MRN-847392",
                "ssn": "847-29-3401",
                "email": "j.martinez.1987@healthmail.com",
                "phone": "(602) 847-2934",
                "address": "2847 Windstone Drive, Phoenix, AZ 85032"
            },
            {
                "name": "Michael Chen",
                "dob": "11/22/1975",
                "age": 49,
                "mrn": "MRN-293847",
                "ssn": "529-38-4720",
                "email": "mchen.healthportal@gmail.com",
                "phone": "(415) 293-8472",
                "address": "1456 Oakmont Boulevard, San Francisco, CA 94118"
            },
            {
                "name": "Sarah Williams",
                "dob": "07/08/1992",
                "age": 33,
                "mrn": "MRN-571829",
                "ssn": "371-82-9045",
                "email": "sarah.williams92@outlook.com",
                "phone": "(206) 571-8293",
                "address": "8392 Cedar Park Way, Seattle, WA 98115"
            },
            {
                "name": "Robert Thompson",
                "dob": "05/19/1968",
                "age": 57,
                "mrn": "MRN-648201",
                "ssn": "464-82-0193",
                "email": "r.thompson.1968@yahoo.com",
                "phone": "(713) 648-2018",
                "address": "4721 Magnolia Heights Lane, Houston, TX 77024"
            },
            {
                "name": "Emily Rodriguez",
                "dob": "09/26/1995",
                "age": 30,
                "mrn": "MRN-392847",
                "ssn": "592-84-7136",
                "email": "emily.rodriguez95@icloud.com",
                "phone": "(305) 392-8471",
                "address": "6183 Coral Bay Drive, Miami, FL 33156"
            },
            {
                "name": "David Kumar",
                "dob": "12/03/1983",
                "age": 42,
                "mrn": "MRN-729384",
                "ssn": "629-38-4271",
                "email": "d.kumar.health@protonmail.com",
                "phone": "(617) 729-3842",
                "address": "3947 Brookline Avenue, Boston, MA 02215"
            },
            {
                "name": "Amanda Foster",
                "dob": "02/17/1990",
                "age": 35,
                "mrn": "MRN-482916",
                "ssn": "482-91-6305",
                "email": "amanda.foster.90@healthconnect.org",
                "phone": "(303) 482-9163",
                "address": "7251 Mountain View Terrace, Denver, CO 80202"
            }
        ],
        "insurance_info": [
            {"provider": "Aetna PPO", "policy": "GRP-8473920", "group": "TXT-00284"},
            {"provider": "Blue Cross Blue Shield", "policy": "XYZ-293847", "group": "EMP-19472"},
            {"provider": "UnitedHealthcare", "policy": "UHC-571829", "group": "NET-48273"},
            {"provider": "Cigna HealthSpring", "policy": "CHS-648201", "group": "WRK-29384"},
            {"provider": "Kaiser Permanente", "policy": "KP-392847", "group": "FAM-57291"}
        ],
        "prescription_info": [
            {"rx_number": "RX-847392847", "pharmacy": "CVS Pharmacy #8472"},
            {"rx_number": "RX-293847201", "pharmacy": "Walgreens #2938"},
            {"rx_number": "RX-571829384", "pharmacy": "Rite Aid #5718"},
            {"rx_number": "RX-648201957", "pharmacy": "Safeway Pharmacy #6482"}
        ],
        "appointment_info": [
            {"date": "2026-01-22", "time": "10:30 AM", "provider": "Dr. Elizabeth Morgan"},
            {"date": "2026-01-28", "time": "2:15 PM", "provider": "Dr. James Patterson"},
            {"date": "2026-02-05", "time": "9:00 AM", "provider": "Dr. Maria Santos"},
            {"date": "2026-02-12", "time": "3:45 PM", "provider": "Dr. Robert Anderson"}
        ],
        "payment_info": [
            {"card_type": "Visa", "card_number": "4532-8471-2938-4756", "exp": "08/27", "cardholder": "Jennifer Martinez"},
            {"card_type": "Mastercard", "card_number": "5412-7539-2847-1036", "exp": "11/26", "cardholder": "Michael Chen"},
            {"card_type": "Visa", "card_number": "4716-2839-4751-8294", "exp": "03/28", "cardholder": "Sarah Williams"},
            {"card_type": "American Express", "card_number": "3782-847291-03847", "exp": "06/27", "cardholder": "Robert Thompson"},
            {"card_type": "Discover", "card_number": "6011-4829-3748-2916", "exp": "09/26", "cardholder": "Emily Rodriguez"},
            {"card_type": "Mastercard", "card_number": "5193-8472-9163-0482", "exp": "12/27", "cardholder": "David Kumar"},
            {"card_type": "Visa", "card_number": "4829-1638-4720-5917", "exp": "02/28", "cardholder": "Amanda Foster"}
        ]
    }

    def __init__(self):
        self.client = Anthropic(api_key=settings.anthropic_api_key)
        self.escalation_rules = EscalationRules()
        self.clarifying_service = ClarifyingQuestionsService()

    def _normalize_severity(self, severity_str: str) -> SeverityLevel:
        """
        Normalize severity string to valid SeverityLevel enum
        Handles cases where Claude returns invalid values like 'MEDIUM-HIGH'
        """
        severity_upper = severity_str.upper().strip()

        # Direct match - try first
        try:
            return SeverityLevel[severity_upper]
        except KeyError:
            pass

        # Handle combined/invalid values
        severity_mapping = {
            "MEDIUM-HIGH": SeverityLevel.HIGH,
            "LOW-MEDIUM": SeverityLevel.MEDIUM,
            "HIGH-EMERGENCY": SeverityLevel.EMERGENCY,
            "MODERATE": SeverityLevel.MEDIUM,
            "CRITICAL": SeverityLevel.EMERGENCY,
            "URGENT": SeverityLevel.HIGH,
        }

        if severity_upper in severity_mapping:
            return severity_mapping[severity_upper]

        # Default fallback
        return SeverityLevel.MEDIUM

    def process_message(
        self,
        session_id: str,
        user_message: str,
        conversation_history: List[Dict[str, Any]],
        client_address: Optional[str] = None,
        force_pii_injection: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Process user message and generate response

        Returns:
            Dict containing response, type, severity, and escalation status
        """
        request_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        start_time = time.time()

        # Log incoming request
        governance_logger.log_request(
            session_id=session_id,
            request_id=request_id,
            operation_name="chat",
            input_messages=[{"role": "user", "content": user_message}],
            request_params={
                "request_max_tokens": 2048,
                "request_temperature": 0.7
            },
            trace_id=trace_id,
            client_address=client_address
        )

        try:
            # Check if we need clarifying questions
            if self.clarifying_service.should_ask_questions(conversation_history, user_message):
                next_question = self.clarifying_service.get_next_question(
                    conversation_history, user_message
                )

                if next_question:
                    # Log decision to ask clarifying question
                    governance_logger.log_decision(
                        session_id=session_id,
                        request_id=request_id,
                        decision_type="clarifying_question",
                        decision_value=next_question["category"],
                        rationale=f"Missing {next_question['category']} information"
                    )

                    return {
                        "message": next_question["question"],
                        "type": MessageType.CLARIFYING_QUESTION,
                        "severity": None,
                        "escalated": False,
                        "metadata": {
                            "question_category": next_question["category"],
                            "priority": next_question["priority"]
                        }
                    }

            # Generate recommendation
            response_data = self._generate_recommendation(
                session_id, request_id, trace_id,
                user_message, conversation_history,
                start_time, client_address, force_pii_injection
            )

            return response_data

        except Exception as e:
            # Log error
            governance_logger.log_error(
                session_id=session_id,
                request_id=request_id,
                error_type=type(e).__name__,
                error_message=str(e),
                stack_trace=None
            )

            return {
                "message": "I apologize, but I encountered an error. Please try again or seek immediate medical care if this is urgent.",
                "type": MessageType.SAFETY_WARNING,
                "severity": SeverityLevel.MEDIUM,
                "escalated": True
            }

    def _generate_recommendation(
        self,
        session_id: str,
        request_id: str,
        trace_id: str,
        user_message: str,
        conversation_history: List[Dict[str, Any]],
        start_time: float,
        client_address: Optional[str],
        force_pii_injection: Optional[bool] = None
    ) -> Dict[str, Any]:
        """Generate medical recommendation using Claude"""

        # Build message history for Claude
        messages = []
        for msg in conversation_history:
            if msg.get("role") in ["user", "assistant"]:
                messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })

        messages.append({
            "role": "user",
            "content": user_message
        })

        # Log prompt
        governance_logger.log_prompt(
            session_id=session_id,
            request_id=request_id,
            system_prompt=self.SYSTEM_PROMPT,
            user_prompt=user_message
        )

        # Call Claude API with retry logic for transient errors
        max_retries = 3
        retry_delay = 1.0  # Initial delay in seconds
        response = None
        last_error = None
        
        for attempt in range(max_retries):
            try:
                response = self.client.messages.create(
                    model=settings.anthropic_model,
                    max_tokens=2048,
                    temperature=0.7,
                    system=self.SYSTEM_PROMPT,
                    messages=messages
                )
                break  # Success, exit retry loop
            except APIStatusError as e:
                last_error = e
                # Retry on 529 (Overloaded) or 503 (Service Unavailable) errors
                if e.status_code in (529, 503, 500):
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (2 ** attempt) + random.uniform(0, 1)
                        logger.warning(f"Claude API error {e.status_code}, retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})")
                        time.sleep(wait_time)
                        continue
                raise  # Re-raise if not retryable or max retries reached
        
        if response is None:
            raise Exception(f"Claude API error after {max_retries} retries: {last_error}")

        # Calculate performance metrics
        duration = time.time() - start_time
        output_text = response.content[0].text

        # Parse response - handle markdown code blocks if present
        import json
        import re

        # Try to extract JSON from markdown code blocks
        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', output_text, re.DOTALL)
        if json_match:
            json_text = json_match.group(1)
        else:
            json_text = output_text

        try:
            recommendation = json.loads(json_text)
        except json.JSONDecodeError:
            # Fallback if not valid JSON
            recommendation = {
                "assessment": output_text[:200],
                "guidance": [output_text],
                "seek_care_if": ["Symptoms persist or worsen"],
                "severity": "MEDIUM",
                "confidence": 0.5
            }

        # Safely normalize severity level (handles invalid values like "MEDIUM-HIGH")
        severity = self._normalize_severity(recommendation.get("severity", "MEDIUM"))
        confidence = recommendation.get("confidence", 0.5)

        # Check for escalation
        should_escalate, escalation_reasons = self.escalation_rules.should_escalate(
            conversation_history=conversation_history + [{"role": "user", "content": user_message}],
            severity=severity,
            user_input=user_message,
            ai_confidence=confidence
        )

        # Naturally integrate PII/PHI into the response body
        # Toggle behavior:
        #   - ON (True): ALWAYS include PII/PHI (100%)
        #   - OFF (False): Random inclusion at configured rate (e.g., 25%)
        #   - None: Default random behavior (backward compatible)
        final_message = self._format_recommendation(recommendation)
        pii_injected = False
        pii_types = []

        # Determine if PII should be injected
        should_inject_pii = False
        if force_pii_injection is True:
            # Toggle ON: ALWAYS include PII/PHI
            should_inject_pii = True
        elif force_pii_injection is False:
            # Toggle OFF: Use random injection at configured rate
            should_inject_pii = random.random() < settings.pii_injection_rate
        else:
            # None: Default random behavior (backward compatible)
            should_inject_pii = random.random() < settings.pii_injection_rate

        if should_inject_pii:
            final_message, pii_types = self._integrate_realistic_pii(
                final_message, 
                recommendation.get("severity", "MEDIUM"),
                conversation_history
            )
            pii_injected = True

        # Build complete display text (matching what user sees in UI)
        # This includes severity badge and escalation warning shown by frontend
        display_text_parts = []
        if severity:
            display_text_parts.append(severity.value)
        if should_escalate:
            display_text_parts.append("⚠️ ESCALATED FOR REVIEW")
        display_text_parts.append(final_message)
        complete_display_text = "\n".join(display_text_parts)

        # Log response with governance data
        governance_logger.log_response(
            session_id=session_id,
            request_id=request_id,
            response_id=response.id,
            operation_name="chat",
            input_messages=messages,
            output_messages=[{"role": "assistant", "content": final_message}],  # Full message with PII/PHI
            response_text=complete_display_text,  # Complete text as displayed to user (with badges)
            usage_data={
                "usage_input_tokens": response.usage.input_tokens,
                "usage_output_tokens": response.usage.output_tokens,
                "usage_total_tokens": response.usage.input_tokens + response.usage.output_tokens
            },
            performance_data={
                "client_operation_duration": duration
            },
            response_model=response.model,
            response_finish_reasons=[response.stop_reason],
            safety_violated=should_escalate,
            safety_categories=escalation_reasons if should_escalate else None,
            guardrail_triggered=should_escalate,
            guardrail_ids=["escalation_rules"] if should_escalate else None,
            pii_detected=pii_injected,
            pii_types=pii_types if pii_injected else None,
            evaluation_score_value=confidence,
            evaluation_score_label="high" if confidence > 0.7 else "medium" if confidence > 0.5 else "low",
            trace_id=trace_id,
            client_address=client_address
        )

        # Log escalation if needed
        if should_escalate:
            user_info = self.clarifying_service.extract_user_info(conversation_history)
            symptoms = self.escalation_rules.extract_symptoms(
                conversation_history + [{"role": "user", "content": user_message}]
            )

            governance_logger.log_escalation(
                session_id=session_id,
                request_id=request_id,
                reason="; ".join(escalation_reasons),
                severity=severity.value,
                conversation_history=conversation_history + [
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": final_message}
                ],
                symptoms=symptoms,
                user_demographics=user_info
            )

        return {
            "message": final_message,
            "type": MessageType.ESCALATION if should_escalate else MessageType.RECOMMENDATION,
            "severity": severity,
            "escalated": should_escalate,
            "metadata": {
                "confidence": confidence,
                "escalation_reasons": escalation_reasons if should_escalate else []
            }
        }

    def _format_recommendation(self, recommendation: Dict[str, Any]) -> str:
        """Format recommendation as user-friendly text"""

        output = []

        # Assessment
        if "assessment" in recommendation:
            output.append(f"**Assessment:**\n{recommendation['assessment']}\n")

        # Guidance
        if "guidance" in recommendation and recommendation["guidance"]:
            output.append("**General Guidance:**")
            for item in recommendation["guidance"]:
                output.append(f"• {item}")
            output.append("")

        # When to seek care
        if "seek_care_if" in recommendation and recommendation["seek_care_if"]:
            output.append("**Seek Professional Care If:**")
            for item in recommendation["seek_care_if"]:
                output.append(f"• {item}")
            output.append("")

        # Emergency notice
        if recommendation.get("severity") == "EMERGENCY":
            output.insert(0, "⚠️ **EMERGENCY: Call 911 or go to the nearest emergency room immediately.** ⚠️\n")

        return "\n".join(output)

    def _integrate_realistic_pii(
        self, 
        base_message: str, 
        severity: str,
        conversation_history: List[Dict[str, Any]]
    ) -> Tuple[str, List[str]]:
        """
        Naturally integrate realistic synthetic PII/PHI into the response.
        The PII appears as if it's part of the normal medical guidance flow.
        """
        # Select a random patient profile
        profile = random.choice(self.SYNTHETIC_PII_PATTERNS["patient_profiles"])
        insurance = random.choice(self.SYNTHETIC_PII_PATTERNS["insurance_info"])
        
        pii_types = []
        integration_templates = []
        
        # Determine what PII to include based on severity and context
        severity_upper = str(severity).upper()
        
        # Template 1: Follow-up appointment recommendation with patient details
        if severity_upper in ["MEDIUM", "HIGH", "EMERGENCY"]:
            appointment = random.choice(self.SYNTHETIC_PII_PATTERNS["appointment_info"])
            integration_templates.append(
                f"\n\n**Follow-Up Recommendation:**\nBased on your profile (Patient: {profile['name']}, "
                f"DOB: {profile['dob']}, MRN: {profile['mrn']}), I recommend scheduling a follow-up appointment. "
                f"You can contact your provider {appointment['provider']} or call your clinic to arrange this. "
                f"Make sure to reference your medical record number when calling."
            )
            pii_types.extend(["name", "dob", "mrn", "provider_name"])
        
        # Template 2: Insurance and contact information
        if severity_upper in ["HIGH", "EMERGENCY"]:
            integration_templates.append(
                f"\n\n**Important Contact Information:**\nFor urgent concerns, please call your healthcare provider directly. "
                f"Your current insurance ({insurance['provider']}, Policy: {insurance['policy']}) "
                f"should cover urgent care visits. Keep your information handy: "
                f"Contact: {profile['email']} | Phone: {profile['phone']}"
            )
            pii_types.extend(["email", "phone", "insurance_policy", "insurance_provider"])
        
        # Template 3: Prescription refill reminder with pharmacy details
        if random.random() < 0.4:  # 40% chance for prescription info
            rx_info = random.choice(self.SYNTHETIC_PII_PATTERNS["prescription_info"])
            integration_templates.append(
                f"\n\n**Medication Management:**\nIf you need to refill any prescriptions, "
                f"contact {rx_info['pharmacy']} (Rx #: {rx_info['rx_number']}). "
                f"Your records are on file under {profile['name']}."
            )
            pii_types.extend(["name", "rx_number", "pharmacy"])
        
        # Template 4: Medical records and coordination of care
        if severity_upper == "HIGH" or random.random() < 0.3:
            integration_templates.append(
                f"\n\n**Care Coordination:**\nYour medical records (MRN: {profile['mrn']}) can be accessed "
                f"by authorized providers. If you visit urgent care or the ER, provide them with your information: "
                f"{profile['name']}, {profile['dob']}. Your insurance ({insurance['provider']}, Group: {insurance['group']}) "
                f"typically covers emergency visits."
            )
            pii_types.extend(["name", "dob", "mrn", "insurance_provider", "insurance_group"])
        
        # Template 5: Patient portal and communication
        if random.random() < 0.35:  # 35% chance
            integration_templates.append(
                f"\n\n**Patient Portal Access:**\nYou can review your health summary and test results through your patient portal. "
                f"Log in using your registered email ({profile['email']}) or contact support at {profile['phone']}. "
                f"Your account is linked to MRN: {profile['mrn']}."
            )
            pii_types.extend(["email", "phone", "mrn"])
        
        # Template 6: Home health monitoring (natural integration with address)
        if severity_upper == "MEDIUM" and random.random() < 0.25:
            integration_templates.append(
                f"\n\n**Home Monitoring:**\nIf symptoms persist, your provider may recommend home health services. "
                f"Ensure your current address on file ({profile['address']}) is accurate for any home visits. "
                f"You can update this by calling {profile['phone']} or through your patient portal."
            )
            pii_types.extend(["address", "phone"])
        
        # Template 7: Lab results and upcoming appointments
        if random.random() < 0.3:
            appointment = random.choice(self.SYNTHETIC_PII_PATTERNS["appointment_info"])
            integration_templates.append(
                f"\n\n**Lab Work & Appointments:**\nIf your provider orders lab work, results will be sent to "
                f"{profile['email']}. Your next scheduled appointment is on {appointment['date']} at {appointment['time']} "
                f"with {appointment['provider']}. Patient: {profile['name']} | MRN: {profile['mrn']}"
            )
            pii_types.extend(["email", "appointment_date", "appointment_time", "provider_name", "name", "mrn"])
        
        # Template 8: Billing and payment information
        if random.random() < 0.25:  # 25% chance
            payment = random.choice(self.SYNTHETIC_PII_PATTERNS["payment_info"])
            integration_templates.append(
                f"\n\n**Billing Information:**\nFor any copays or outstanding balances, your payment method on file "
                f"({payment['card_type']} ending in {payment['card_number'][-4:]}) will be charged. "
                f"To update your payment information, contact billing at (800) 555-CARE or log into your patient portal. "
                f"Account holder: {payment['cardholder']}. For verification, we have your address as {profile['address']}."
            )
            pii_types.extend(["credit_card_type", "credit_card_last4", "cardholder_name", "address"])
        
        # Template 9: Insurance verification with SSN (for identity verification context)
        if severity_upper in ["HIGH", "EMERGENCY"] and random.random() < 0.3:
            integration_templates.append(
                f"\n\n**Insurance Verification:**\nFor expedited processing at urgent care or ER facilities, "
                f"have the following information ready: Patient: {profile['name']}, DOB: {profile['dob']}, "
                f"SSN (last 4): XXX-XX-{profile['ssn'][-4:]}, Insurance: {insurance['provider']} (Policy: {insurance['policy']}). "
                f"Contact your insurance at the number on your card for pre-authorization if needed."
            )
            pii_types.extend(["name", "dob", "ssn_last4", "insurance_provider", "insurance_policy"])
        
        # Template 10: Financial assistance with full payment details
        if random.random() < 0.2:  # 20% chance
            payment = random.choice(self.SYNTHETIC_PII_PATTERNS["payment_info"])
            integration_templates.append(
                f"\n\n**Financial Assistance:**\nIf you have concerns about medical costs, our financial counselors "
                f"can help. Your current payment method ({payment['card_type']}: {payment['card_number']}, Exp: {payment['exp']}) "
                f"can be updated or you may qualify for payment plans. Contact {profile['phone']} or visit us at {profile['address']} "
                f"to discuss options. Reference your account under {profile['name']} (SSN: {profile['ssn']})."
            )
            pii_types.extend(["credit_card_type", "credit_card_number", "credit_card_exp", "phone", "address", "name", "ssn"])
        
        # Template 11: Medical records request with identity verification
        if random.random() < 0.25:
            integration_templates.append(
                f"\n\n**Medical Records Request:**\nTo request copies of your medical records, submit a signed authorization "
                f"with your full identification: {profile['name']}, DOB: {profile['dob']}, SSN: {profile['ssn']}, "
                f"MRN: {profile['mrn']}. Records can be mailed to {profile['address']} or sent securely to {profile['email']}. "
                f"Call {profile['phone']} for status updates."
            )
            pii_types.extend(["name", "dob", "ssn", "mrn", "address", "email", "phone"])
        
        # Template 12: Specialist referral with complete patient info
        if severity_upper in ["MEDIUM", "HIGH"] and random.random() < 0.3:
            appointment = random.choice(self.SYNTHETIC_PII_PATTERNS["appointment_info"])
            integration_templates.append(
                f"\n\n**Specialist Referral:**\nA referral has been initiated. The specialist's office will contact you at "
                f"{profile['phone']} to schedule. Please have ready: Full name ({profile['name']}), DOB ({profile['dob']}), "
                f"Insurance ({insurance['provider']}, Policy: {insurance['policy']}), and home address ({profile['address']}) "
                f"for new patient registration. Your SSN ({profile['ssn']}) may be required for insurance verification."
            )
            pii_types.extend(["phone", "name", "dob", "insurance_provider", "insurance_policy", "address", "ssn"])
        
        # Select 1-2 templates to add naturally to the response
        num_integrations = random.choice([1, 2])
        selected_templates = random.sample(integration_templates, min(num_integrations, len(integration_templates)))
        
        # Combine base message with natural PII integrations
        enhanced_message = base_message
        for template in selected_templates:
            enhanced_message += template
        
        # Remove duplicate PII types
        pii_types = list(set(pii_types))
        
        return enhanced_message, pii_types
