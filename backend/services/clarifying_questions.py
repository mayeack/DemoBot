from typing import List, Dict, Any, Optional
import re

class ClarifyingQuestionsService:
    """Manage clarifying questions logic - only ask when truly needed for confident recommendations"""

    MAX_QUESTIONS = 2  # Reduced from 3 to minimize unnecessary questions

    # Only ask critical questions that significantly impact safety and recommendations
    CRITICAL_QUESTIONS = [
        {
            "category": "emergency",
            "condition": lambda text: any(word in text.lower() for word in [
                "chest pain", "difficulty breathing", "unconscious", "severe bleeding",
                "stroke", "heart attack", "suicide", "overdose"
            ]),
            "question": "⚠️ Is this an emergency situation happening right now? If yes, please call 911 or go to the nearest emergency room immediately."
        },
        {
            "category": "pediatric",
            "condition": lambda text: (
                # Only ask age if indicators suggest infant/child AND no age mentioned
                any(word in text.lower() for word in ["infant", "baby", "child", "kid"]) and
                not any(re.search(pattern, text, re.IGNORECASE) for pattern in [
                    r"\b\d+\s*(?:years?|months?|weeks?)\s*old\b",
                    r"\bage\s*(?:is|:)?\s*\d+"
                ])
            ),
            "question": "What is the child's age? This is critical for appropriate pediatric guidance."
        },
        {
            "category": "symptoms_needed",
            "condition": lambda text: (
                # Vague health concerns without specific symptoms
                (any(phrase in text.lower() for phrase in ["not feeling well", "feel sick", "feel good", "feeling well", "dont feel", "unwell", "feeling ill"]) or
                 re.search(r"\b(?:sick|ill)\b", text.lower())) and
                not any(word in text.lower() for word in [
                    "headache", "fever", "pain", "cough", "nausea", "vomiting", "diarrhea",
                    "sore throat", "dizzy", "tired", "fatigue", "ache", "bleeding", "rash",
                    "swelling", "runny nose", "congestion"
                ])
            ),
            "question": "Could you describe your specific symptoms? For example: fever, pain, nausea, cough, etc. This will help me provide more appropriate guidance."
        }
    ]

    # Removed most "important" questions - the AI can provide recommendations without this info
    IMPORTANT_QUESTIONS = []

    def __init__(self):
        self.questions_asked = []

    def should_ask_questions(
        self,
        conversation_history: List[Dict[str, Any]],
        user_input: str
    ) -> bool:
        """Determine if we need to ask clarifying questions - only for critical safety issues"""

        # Count questions already asked
        questions_count = sum(
            1 for msg in conversation_history
            if msg.get("role") == "assistant" and "?" in msg.get("content", "")
        )

        # Don't ask if we've reached max
        if questions_count >= self.MAX_QUESTIONS:
            return False

        # Check if we need critical information
        full_text = self._get_full_conversation_text(conversation_history, user_input)

        # ONLY ask if missing CRITICAL info that impacts safety
        for q in self.CRITICAL_QUESTIONS:
            if q["condition"](full_text) and q["category"] not in [
                msg.get("metadata", {}).get("question_category")
                for msg in conversation_history
            ]:
                return True

        # No longer ask "important" questions - let the AI work with what it has
        return False

    def get_next_question(
        self,
        conversation_history: List[Dict[str, Any]],
        user_input: str
    ) -> Optional[Dict[str, str]]:
        """Get the next critical clarifying question - only if truly needed"""

        full_text = self._get_full_conversation_text(conversation_history, user_input)
        asked_categories = [
            msg.get("metadata", {}).get("question_category")
            for msg in conversation_history
            if msg.get("role") == "assistant"
        ]

        # Only check critical questions - nothing else
        for q in self.CRITICAL_QUESTIONS:
            if q["category"] not in asked_categories and q["condition"](full_text):
                return {
                    "question": q["question"],
                    "category": q["category"],
                    "priority": "critical"
                }

        return None

    def _get_full_conversation_text(
        self,
        conversation_history: List[Dict[str, Any]],
        current_input: str
    ) -> str:
        """Get all conversation text for analysis"""
        texts = [current_input]
        for msg in conversation_history:
            if isinstance(msg, dict) and "content" in msg:
                texts.append(msg["content"])
        return " ".join(texts)

    def extract_user_info(
        self,
        conversation_history: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Extract user demographic and medical info from conversation"""

        full_text = " ".join([
            msg.get("content", "")
            for msg in conversation_history
            if msg.get("role") == "user"
        ])

        user_info = {}

        # Extract age
        age_match = re.search(r"\b(\d+)\s*(?:years?|yrs?)\s*old\b", full_text, re.IGNORECASE)
        if age_match:
            user_info["age"] = int(age_match.group(1))

        # Extract sex/gender
        if re.search(r"\b(?:female|woman|girl)\b", full_text, re.IGNORECASE):
            user_info["sex"] = "female"
        elif re.search(r"\b(?:male|man|boy)\b", full_text, re.IGNORECASE):
            user_info["sex"] = "male"

        # Extract pregnancy status
        if re.search(r"\bpregnant\b", full_text, re.IGNORECASE):
            user_info["pregnant"] = True

        # Extract medications
        med_mentions = re.findall(
            r"taking\s+([\w\s,]+?)(?:\.|for|to|because)",
            full_text,
            re.IGNORECASE
        )
        if med_mentions:
            user_info["medications"] = [m.strip() for m in med_mentions]

        # Extract chronic conditions
        condition_keywords = [
            "diabetes", "hypertension", "high blood pressure",
            "asthma", "heart disease", "copd", "arthritis"
        ]
        conditions = [
            cond for cond in condition_keywords
            if cond in full_text.lower()
        ]
        if conditions:
            user_info["chronic_conditions"] = conditions

        # Extract allergies
        allergy_match = re.search(
            r"allergic\s+to\s+([\w\s,]+?)(?:\.|and|$)",
            full_text,
            re.IGNORECASE
        )
        if allergy_match:
            user_info["allergies"] = [a.strip() for a in allergy_match.group(1).split(",")]

        return user_info
