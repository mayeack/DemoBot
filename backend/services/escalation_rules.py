from typing import List, Dict, Any, Optional, Tuple
import re
from backend.models.schemas import SeverityLevel

class EscalationRules:
    """Determine if consultation should be escalated for human review"""

    # Emergency symptoms requiring immediate medical attention
    EMERGENCY_SYMPTOMS = [
        "chest pain", "difficulty breathing", "severe shortness of breath",
        "stroke", "paralysis", "loss of consciousness", "unconscious",
        "severe bleeding", "severe head injury", "seizure",
        "suicidal", "self-harm", "suicide", "kill myself",
        "severe allergic reaction", "anaphylaxis",
        "severe abdominal pain", "coughing up blood", "vomiting blood",
        "sudden vision loss", "sudden weakness", "slurred speech",
        "severe burns", "poisoning", "overdose"
    ]

    # High-risk symptoms
    HIGH_RISK_SYMPTOMS = [
        "persistent fever", "high fever", "fever over 103",
        "severe pain", "chronic pain", "worsening pain",
        "infection", "infected wound",
        "difficulty swallowing", "persistent vomiting", "severe diarrhea",
        "blood in stool", "blood in urine",
        "rapid heartbeat", "irregular heartbeat", "palpitations",
        "confusion", "disorientation", "memory loss",
        "severe headache", "migraine", "worst headache"
    ]

    # Vulnerable populations (extracted from conversation)
    AGE_RISK_PATTERNS = [
        r"\b(?:infant|baby|newborn|toddler)\b",
        r"\b(?:under|less than|younger than)\s*(?:2|two)\s*(?:years?|months?)\b",
        r"\b(?:elderly|senior|over|older than)\s*(?:65|seventy|eighty|ninety)\b"
    ]

    PREGNANCY_PATTERNS = [
        r"\bpregnant\b", r"\bpregnancy\b", r"\bexpecting\b",
        r"\btrimester\b", r"\bdue date\b"
    ]

    # Drug interaction indicators
    MEDICATION_PATTERNS = [
        r"\btaking\s+\w+\s+medication\b",
        r"\bon\s+\w+\s+(?:drug|medicine)\b",
        r"\bprescription\b"
    ]

    def __init__(self):
        self.escalation_reasons = []

    def should_escalate(
        self,
        conversation_history: List[Dict[str, Any]],
        severity: SeverityLevel,
        user_input: str,
        ai_confidence: Optional[float] = None
    ) -> Tuple[bool, List[str]]:
        """
        Determine if consultation should be escalated

        Returns:
            Tuple of (should_escalate: bool, reasons: List[str])
        """
        self.escalation_reasons = []
        full_conversation = self._extract_full_text(conversation_history)

        # Check emergency symptoms
        if self._check_emergency_symptoms(user_input, full_conversation):
            self.escalation_reasons.append("Emergency symptoms detected")

        # Check severity level
        if severity in [SeverityLevel.EMERGENCY, SeverityLevel.HIGH]:
            self.escalation_reasons.append(f"High severity level: {severity.value}")

        # Check vulnerable populations
        if self._check_age_risk(full_conversation):
            self.escalation_reasons.append("Vulnerable age group identified")

        if self._check_pregnancy(full_conversation):
            self.escalation_reasons.append("Pregnancy detected")

        # Check for medication interactions
        if self._check_medication_risk(full_conversation):
            self.escalation_reasons.append("Potential medication interactions")

        # Check for persistent/worsening symptoms
        if self._check_persistent_symptoms(full_conversation):
            self.escalation_reasons.append("Persistent or worsening symptoms")

        # Check for self-harm ideation
        if self._check_self_harm(user_input, full_conversation):
            self.escalation_reasons.append("Self-harm ideation expressed")

        # REMOVED: Low AI confidence escalation - instead ask clarifying questions
        # Low confidence should trigger clarifying questions, not escalation
        # Only escalate for specific, pre-defined safety concerns

        # Check for explicit human review request
        if self._check_human_review_request(user_input, full_conversation):
            self.escalation_reasons.append("User requested professional review")

        return len(self.escalation_reasons) > 0, self.escalation_reasons

    def _extract_full_text(self, conversation_history: List[Dict[str, Any]]) -> str:
        """Extract only USER text from conversation history to avoid false positives from AI responses"""
        texts = []
        for msg in conversation_history:
            if isinstance(msg, dict) and "content" in msg and msg.get("role") == "user":
                texts.append(msg["content"])
        return " ".join(texts).lower()

    def _check_emergency_symptoms(self, user_input: str, full_text: str) -> bool:
        """Check for emergency symptoms"""
        text = (user_input + " " + full_text).lower()
        for symptom in self.EMERGENCY_SYMPTOMS:
            if symptom in text:
                return True
        return False

    def _check_age_risk(self, full_text: str) -> bool:
        """Check for vulnerable age groups"""
        for pattern in self.AGE_RISK_PATTERNS:
            if re.search(pattern, full_text, re.IGNORECASE):
                return True
        return False

    def _check_pregnancy(self, full_text: str) -> bool:
        """Check for pregnancy"""
        for pattern in self.PREGNANCY_PATTERNS:
            if re.search(pattern, full_text, re.IGNORECASE):
                return True
        return False

    def _check_medication_risk(self, full_text: str) -> bool:
        """Check for potential medication interactions"""
        # Count medication mentions
        medication_count = 0
        for pattern in self.MEDICATION_PATTERNS:
            matches = re.findall(pattern, full_text, re.IGNORECASE)
            medication_count += len(matches)

        # Escalate if multiple medications mentioned
        return medication_count >= 2

    def _check_persistent_symptoms(self, full_text: str) -> bool:
        """Check for persistent or worsening symptoms"""
        persistent_indicators = [
            "getting worse", "worsening", "not getting better",
            "still have", "persistent", "for weeks", "for months",
            "keeps happening", "recurring", "chronic"
        ]

        for indicator in persistent_indicators:
            if indicator in full_text:
                return True
        return False

    def _check_self_harm(self, user_input: str, full_text: str) -> bool:
        """Check for self-harm ideation"""
        text = (user_input + " " + full_text).lower()
        self_harm_patterns = [
            "kill myself", "end my life", "suicide", "suicidal",
            "self-harm", "hurt myself", "don't want to live"
        ]

        for pattern in self_harm_patterns:
            if pattern in text:
                return True
        return False

    def _check_human_review_request(self, user_input: str, full_text: str) -> bool:
        """Check if user explicitly requested human review"""
        text = (user_input + " " + full_text).lower()
        review_requests = [
            "talk to a doctor", "see a doctor", "speak to a professional",
            "human review", "real doctor", "medical professional",
            "need a doctor", "want to see a doctor"
        ]

        for request in review_requests:
            if request in text:
                return True
        return False

    def extract_symptoms(self, conversation_history: List[Dict[str, Any]]) -> List[str]:
        """Extract mentioned symptoms from conversation"""
        full_text = self._extract_full_text(conversation_history)
        symptoms = []

        # Check emergency symptoms
        for symptom in self.EMERGENCY_SYMPTOMS:
            if symptom in full_text:
                symptoms.append(symptom)

        # Check high-risk symptoms
        for symptom in self.HIGH_RISK_SYMPTOMS:
            if symptom in full_text:
                symptoms.append(symptom)

        return list(set(symptoms))  # Remove duplicates
