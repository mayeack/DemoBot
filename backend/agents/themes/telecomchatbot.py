"""TelecomChatbot theme - synthetic wireless/internet support (conversational).

This theme is conversational: it skips the medical clarifier and renders the
model's free-text ``reply`` verbatim. The specialists still produce internal
findings; only the synthesizer's ``reply`` is shown to the customer.
"""

from backend.agents.themes.base import (
    SpecialistSpec,
    ThemeConfig,
    is_conversational,
    prompt_for,
)

SPECIALISTS = (
    SpecialistSpec(
        key="network_diagnostics",
        label="Network Diagnostics",
        role="cell/data/Wi-Fi signal and connectivity troubleshooting",
        focus=(
            "diagnosing cell service, mobile data, and home Wi-Fi/router connectivity "
            "problems and the basic troubleshooting steps that usually help"
        ),
    ),
    SpecialistSpec(
        key="billing",
        label="Billing & Plans",
        role="general billing and plan questions (no real account access)",
        focus=(
            "general billing and plan questions, while making clear the assistant "
            "cannot view or change any real account and must defer such changes"
        ),
    ),
    SpecialistSpec(
        key="device_setup",
        label="Device Setup",
        role="restarts, updates, SIM/eSIM, and network-reset steps",
        focus=(
            "device basics — restarts, software updates, SIM/eSIM reseating, and "
            "network-reset steps the customer can safely perform themselves"
        ),
    ),
    SpecialistSpec(
        key="account_security",
        label="Account Security",
        role="lost/stolen device, fraud, SIM-swap, and escalation",
        focus=(
            "account-security concerns (lost/stolen device, suspected fraud or "
            "SIM-swap) and when to escalate to a human agent or store visit, never "
            "accepting real passwords, PINs, or one-time codes"
        ),
    ),
)

THEME = ThemeConfig(
    key="telecomchatbot",
    label="TelecomChatbot",
    conversational=is_conversational("telecomchatbot"),
    system_prompt=prompt_for("telecomchatbot"),
    specialists=SPECIALISTS,
)
