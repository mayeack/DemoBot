"""FinanceAdvice theme - personal finance literacy (not CFP advice)."""

from backend.agents.themes.base import (
    SpecialistSpec,
    ThemeConfig,
    is_conversational,
    prompt_for,
)

SPECIALISTS = (
    SpecialistSpec(
        key="budgeting",
        label="Budgeting & Cash Flow",
        role="budgeting, saving, and emergency-fund basics",
        focus=(
            "budgeting, cash-flow, saving habits, and emergency-fund basics tailored "
            "to the user's described situation (general literacy, not personal advice)"
        ),
    ),
    SpecialistSpec(
        key="investing",
        label="Investing Basics",
        role="general investing concepts, diversification, and risk",
        focus=(
            "general investing concepts (diversification, risk tolerance, long-term "
            "horizons) without recommending specific securities or guaranteeing returns"
        ),
    ),
    SpecialistSpec(
        key="debt_credit",
        label="Debt & Credit",
        role="debt payoff strategies, credit scores, and interest",
        focus=(
            "debt-payoff strategies, how credit scores and interest work, and "
            "prioritization between competing obligations"
        ),
    ),
    SpecialistSpec(
        key="risk_compliance",
        label="Risk & Red Flags",
        role="scams, high-risk schemes, and when to see a fiduciary",
        focus=(
            "scam/high-risk-scheme red flags, suitability concerns, and clear "
            "indicators the user should consult a licensed fiduciary advisor"
        ),
    ),
)

THEME = ThemeConfig(
    key="financeadvice",
    label="FinanceAdvice",
    conversational=is_conversational("financeadvice"),
    system_prompt=prompt_for("financeadvice"),
    specialists=SPECIALISTS,
)
