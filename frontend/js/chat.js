let sessionId = null;
let disclaimerAccepted = false;
let piiEnabled = false;
let toxicEnabled = false;
let hallucinationEnabled = false;
let aiDefenseEnabled = false;
let internalPolicyEnabled = true;
let autoPromptEnabled = false;
let autoPromptStatusInterval = null;
let currentTheme = 'medadvice';

const THEMES = {
    medadvice: {
        key: 'medadvice',
        label: 'MedAdvice',
        pageTitle: 'MedAdvice v3 - Medical Guidance',
        appTitle: 'MedAdvice v3',
        subtitle: 'General Medical Guidance with AI Governance',
        placeholder: 'Describe your symptoms or concern...',
        welcomeGreeting: 'How can I help you today?',
        welcomeSubtext: 'Please describe your symptoms or health concern.',
        disclaimerHeading: 'IMPORTANT MEDICAL DISCLAIMER',
        disclaimerIntro: 'This service provides general health information and guidance only. It is NOT a substitute for professional medical advice, diagnosis, or treatment.',
        disclaimerPoints: [
            'This is NOT emergency medical care. If you are experiencing a medical emergency, call 911 or go to the nearest emergency room immediately.',
            'The information provided is for educational purposes only and should not be used to diagnose or treat any health condition.',
            'Always seek the advice of your physician or other qualified health provider with any questions you may have regarding a medical condition.',
            'Never disregard professional medical advice or delay in seeking it because of something you have read here.',
            'This service does NOT provide prescription medication advice or pediatric dosing.',
            'If you are pregnant, elderly, or have chronic health conditions, consult with a healthcare provider before following any recommendations.'
        ],
        disclaimerAcknowledge: [
            'You understand this is not professional medical care',
            'You will seek emergency care for urgent symptoms',
            'You will consult a healthcare provider for proper diagnosis and treatment',
            'You understand the limitations of this service'
        ],
        bannerTitle: 'EMERGENCY?',
        bannerText: 'If this is a medical emergency, call 911 or go to your nearest emergency room immediately.',
        showBanner: true,
        piiLabel: 'Include Synthetic PII/PHI in Responses',
        errorFallback: 'Sorry, I encountered an error. Please try again or seek immediate medical care if urgent.',
        primary: '#2563eb',
        primaryHover: '#1d4ed8',
        primaryLight: '#dbeafe',
        primaryRing: '#93bbfd',
        tailwindColor: 'blue',
    },
    taxadvice: {
        key: 'taxadvice',
        label: 'TaxAdvice',
        pageTitle: 'TaxAdvice v3 - Tax Guidance',
        appTitle: 'TaxAdvice v3',
        subtitle: 'General Tax Guidance with AI Governance',
        placeholder: 'Describe your tax question or concern...',
        welcomeGreeting: 'How can I help with your taxes today?',
        welcomeSubtext: 'Please describe your tax question or situation.',
        disclaimerHeading: 'IMPORTANT TAX DISCLAIMER',
        disclaimerIntro: 'This service provides general tax information and guidance only. It is NOT a substitute for professional tax advice from a CPA, enrolled agent, or tax attorney.',
        disclaimerPoints: [
            'This is NOT professional tax preparation. If you have a complex tax situation, consult a licensed tax professional immediately.',
            'The information provided is for educational purposes only and should not be used to file taxes or make financial decisions.',
            'Always seek the advice of a qualified tax professional with any questions about your specific tax situation.',
            'Never disregard professional tax advice or miss filing deadlines because of something you have read here.',
            'This service does NOT prepare tax returns or provide audit representation.',
            'If you are facing IRS enforcement actions, liens, or levies, consult with a tax attorney immediately.'
        ],
        disclaimerAcknowledge: [
            'You understand this is not professional tax advice',
            'You will consult a tax professional for complex situations',
            'You will not rely solely on this service for tax filing decisions',
            'You understand the limitations of this service'
        ],
        bannerTitle: 'URGENT TAX DEADLINE?',
        bannerText: 'If you are facing an imminent IRS deadline, lien, or levy, contact a tax professional or the IRS at 1-800-829-1040 immediately.',
        errorFallback: 'Sorry, I encountered an error. Please try again or consult a tax professional if your situation is urgent.',
        primary: '#059669',
        primaryHover: '#047857',
        primaryLight: '#d1fae5',
        primaryRing: '#6ee7b7',
        tailwindColor: 'emerald',
    },
    benefitsadvice: {
        key: 'benefitsadvice',
        label: 'BenefitsAdvice',
        pageTitle: 'BenefitsAdvice v3 - Benefits Guidance',
        appTitle: 'BenefitsAdvice v3',
        subtitle: 'Employee Benefits Guidance with AI Governance',
        placeholder: 'Ask about your benefits or coverage...',
        welcomeGreeting: 'How can I help with your benefits today?',
        welcomeSubtext: 'Ask about health insurance, retirement, leave policies, or other benefits.',
        disclaimerHeading: 'IMPORTANT BENEFITS DISCLAIMER',
        disclaimerIntro: 'This service provides general employee benefits information only. It is NOT a substitute for your HR department, plan administrator, or benefits specialist.',
        disclaimerPoints: [
            'This is NOT official benefits administration. Contact your HR department for definitive answers about your specific plan.',
            'The information provided is for educational purposes only and should not be used to make enrollment or coverage decisions.',
            'Always verify coverage details with your plan administrator before making healthcare or financial decisions.',
            'Never miss open enrollment or COBRA deadlines because of something you have read here.',
            'This service does NOT process claims, enrollments, or appeals.',
            'If you are experiencing a coverage lapse or urgent benefits issue, contact your HR department immediately.'
        ],
        disclaimerAcknowledge: [
            'You understand this is not official benefits administration',
            'You will verify details with your HR department or plan administrator',
            'You will not miss enrollment deadlines based solely on this service',
            'You understand the limitations of this service'
        ],
        bannerTitle: 'ENROLLMENT DEADLINE?',
        bannerText: 'If you are facing an open enrollment or COBRA deadline, contact your HR department or benefits administrator immediately.',
        errorFallback: 'Sorry, I encountered an error. Please try again or contact your HR department if your situation is urgent.',
        primary: '#7c3aed',
        primaryHover: '#6d28d9',
        primaryLight: '#ede9fe',
        primaryRing: '#c4b5fd',
        tailwindColor: 'violet',
    },
    legaladvice: {
        key: 'legaladvice',
        label: 'LegalAdvice',
        pageTitle: 'LegalAdvice v3 - Legal Guidance',
        appTitle: 'LegalAdvice v3',
        subtitle: 'General Legal Guidance with AI Governance',
        placeholder: 'Describe your legal question or concern...',
        welcomeGreeting: 'How can I help with your legal question today?',
        welcomeSubtext: 'Please describe your legal question or situation.',
        disclaimerHeading: 'IMPORTANT LEGAL DISCLAIMER',
        disclaimerIntro: 'This service provides general legal information only. It is NOT a substitute for professional legal counsel from a licensed attorney. No attorney-client relationship is formed by using this service.',
        disclaimerPoints: [
            'This is NOT legal representation. If you are facing arrest, a court hearing, or legal emergency, contact a licensed attorney immediately.',
            'The information provided is for educational purposes only and should not be used to make legal decisions.',
            'Always seek the advice of a licensed attorney with any questions about your specific legal situation.',
            'Never disregard professional legal advice or miss court deadlines because of something you have read here.',
            'This service does NOT provide case-specific legal strategy or document preparation.',
            'If you are in immediate danger or facing a criminal matter, contact law enforcement (911) or a criminal defense attorney.'
        ],
        disclaimerAcknowledge: [
            'You understand this is not professional legal counsel',
            'You will consult a licensed attorney for actionable legal matters',
            'You understand no attorney-client relationship is formed',
            'You understand the limitations of this service'
        ],
        bannerTitle: 'LEGAL EMERGENCY?',
        bannerText: 'If you are facing arrest, a court deadline, or need immediate legal help, contact a licensed attorney or legal aid service immediately.',
        errorFallback: 'Sorry, I encountered an error. Please try again or consult a licensed attorney if your situation is urgent.',
        primary: '#d97706',
        primaryHover: '#b45309',
        primaryLight: '#fef3c7',
        primaryRing: '#fcd34d',
        tailwindColor: 'amber',
    },
    financeadvice: {
        key: 'financeadvice',
        label: 'FinanceAdvice',
        pageTitle: 'FinanceAdvice v3 - Finance Guidance',
        appTitle: 'FinanceAdvice v3',
        subtitle: 'Personal Finance Guidance with AI Governance',
        placeholder: 'Ask about budgeting, investing, or planning...',
        welcomeGreeting: 'How can I help with your finances today?',
        welcomeSubtext: 'Ask about budgeting, saving, investing, or financial planning.',
        disclaimerHeading: 'IMPORTANT FINANCIAL DISCLAIMER',
        disclaimerIntro: 'This service provides general financial information and guidance only. It is NOT a substitute for professional financial advice from a certified financial planner (CFP) or licensed financial advisor.',
        disclaimerPoints: [
            'This is NOT professional financial planning. If you have complex financial needs, consult a certified financial planner.',
            'The information provided is for educational purposes only and should not be used to make investment or major financial decisions.',
            'Always seek the advice of a qualified financial advisor before making significant financial commitments.',
            'Never make investment decisions solely based on information provided here. Past performance does not guarantee future results.',
            'This service does NOT provide specific investment recommendations, stock picks, or portfolio management.',
            'If you are facing foreclosure, bankruptcy, or financial fraud, consult with a financial advisor or attorney immediately.'
        ],
        disclaimerAcknowledge: [
            'You understand this is not professional financial advice',
            'You will consult a financial advisor for significant decisions',
            'You will not make investment decisions based solely on this service',
            'You understand the limitations of this service'
        ],
        bannerTitle: 'FINANCIAL EMERGENCY?',
        bannerText: 'If you are facing foreclosure, bankruptcy deadlines, or suspect financial fraud, contact a financial advisor or attorney immediately.',
        errorFallback: 'Sorry, I encountered an error. Please try again or consult a financial advisor if your situation is urgent.',
        primary: '#0d9488',
        primaryHover: '#0f766e',
        primaryLight: '#ccfbf1',
        primaryRing: '#5eead4',
        tailwindColor: 'teal',
    },
    telecomchatbot: {
        key: 'telecomchatbot',
        label: 'TelecomChatbot',
        pageTitle: 'Telecom Support - Wireless & Internet Help',
        appTitle: 'Telecom Support',
        subtitle: 'Wireless & Internet Support',
        placeholder: "Tell us what's going on with your service...",
        welcomeGreeting: 'Hi! How can I help with your service today?',
        welcomeSubtext: "Tell me what's going on with your phone, data, or home internet and we'll troubleshoot it together.",
        disclaimerHeading: 'SUPPORT CHAT NOTICE',
        disclaimerIntro: 'This is a synthetic support assistant for general wireless and internet troubleshooting. It is NOT affiliated with Telecom, and it cannot view, verify, or change any real account, billing, or device.',
        disclaimerPoints: [
            'This is a demonstration assistant. Any account numbers, phone numbers, plans, or billing details shown are entirely fictitious.',
            'This chat cannot make real changes to your account, plan, or billing. Contact your carrier directly for account actions.',
            'Never share real passwords, PINs, full card numbers, or one-time security codes in this chat.',
            'Troubleshooting steps are general guidance only and may not match your specific device or plan.',
            'For service outages or coverage in your area, check your carrier’s official status page or app.',
            'If you have a life-threatening emergency and your line is down, call 911 from any available phone.'
        ],
        disclaimerAcknowledge: [
            'You understand this is a synthetic assistant not affiliated with Telecom',
            'You will not share real passwords, PINs, or security codes',
            'You will contact your carrier directly for real account or billing changes',
            'You understand the limitations of this service'
        ],
        bannerTitle: 'EMERGENCY?',
        bannerText: 'If you have a life-threatening emergency and your line is down, call 911 from any available phone or landline immediately.',
        errorFallback: "Sorry, something went wrong on our end. Please try again, or check your carrier's status page if you suspect an outage.",
        primary: '#ee0000',
        primaryHover: '#cd040b',
        primaryLight: '#fee2e2',
        primaryRing: '#fca5a5',
        tailwindColor: 'red',
    }
};

// Track the previous tailwind color for class swaps
let prevTailwindColor = 'blue';

function applyTheme(themeKey) {
    const theme = THEMES[themeKey];
    if (!theme) return;

    currentTheme = themeKey;
    localStorage.setItem('medadvice_theme', themeKey);

    // CSS custom properties for inline <style> rules
    const root = document.documentElement;
    root.style.setProperty('--primary', theme.primary);
    root.style.setProperty('--primary-hover', theme.primaryHover);
    root.style.setProperty('--primary-light', theme.primaryLight);
    root.style.setProperty('--primary-ring', theme.primaryRing);

    // Page title
    document.title = theme.pageTitle;

    // App header
    const appTitle = document.getElementById('appTitle');
    if (appTitle) appTitle.textContent = theme.appTitle;
    const appSubtitle = document.getElementById('appSubtitle');
    if (appSubtitle) appSubtitle.textContent = theme.subtitle;

    // Disclaimer modal
    const disclaimerTitle = document.getElementById('disclaimerTitle');
    if (disclaimerTitle) disclaimerTitle.textContent = theme.disclaimerHeading;
    const disclaimerIntro = document.getElementById('disclaimerIntro');
    if (disclaimerIntro) disclaimerIntro.textContent = theme.disclaimerIntro;
    const disclaimerPoints = document.getElementById('disclaimerPoints');
    if (disclaimerPoints) {
        disclaimerPoints.innerHTML = theme.disclaimerPoints
            .map(p => `<li>${p}</li>`).join('');
    }
    const disclaimerAcknowledge = document.getElementById('disclaimerAcknowledge');
    if (disclaimerAcknowledge) {
        disclaimerAcknowledge.innerHTML = theme.disclaimerAcknowledge
            .map((a, i) => `<li>${a}</li>`).join('');
    }

    // Emergency banner — shown only for themes that opt in (MedAdvice). The
    // "call 911 / emergency room" warning is a medical-safety concept and is
    // hidden for non-medical themes.
    const emergencyBanner = document.getElementById('emergencyBanner');
    if (emergencyBanner) emergencyBanner.style.display = theme.showBanner ? '' : 'none';
    const bannerTitle = document.getElementById('bannerTitle');
    if (bannerTitle) bannerTitle.textContent = theme.bannerTitle;
    const bannerText = document.getElementById('bannerText');
    if (bannerText) bannerText.textContent = theme.bannerText;

    // PII/PHI toggle label — "PHI" (protected health information) applies only
    // to MedAdvice; other themes show just "PII".
    const piiLabel = document.getElementById('piiLabel');
    if (piiLabel) piiLabel.textContent = theme.piiLabel || 'Include Synthetic PII in Responses';

    // Input placeholder
    const messageInput = document.getElementById('messageInput');
    if (messageInput) messageInput.placeholder = theme.placeholder;

    // Welcome message (only if it exists in the chat container)
    const welcomeMsg = document.querySelector('#chatContainer .text-center.text-gray-500');
    if (welcomeMsg) {
        const greeting = welcomeMsg.querySelector('p:first-child');
        const subtext = welcomeMsg.querySelector('p.text-sm');
        if (greeting) greeting.textContent = theme.welcomeGreeting;
        if (subtext) subtext.textContent = theme.welcomeSubtext;
    }

    // Swap Tailwind color classes on themed elements
    const newColor = theme.tailwindColor;
    const colorSwapTargets = [
        { el: appTitle, classes: ['text-{c}-600'] },
        { el: document.getElementById('sendButton'), classes: ['bg-{c}-600', 'hover:bg-{c}-700'] },
        { el: document.getElementById('acceptBtn'), classes: ['bg-{c}-600', 'hover:bg-{c}-700'] },
        { el: document.getElementById('messageInput'), classes: ['focus:ring-{c}-500'] },
    ];

    const footerLinks = document.querySelectorAll('#appFooter a');
    footerLinks.forEach(link => {
        colorSwapTargets.push({ el: link, classes: ['text-{c}-600', 'hover:text-{c}-800'] });
    });

    colorSwapTargets.forEach(({ el, classes }) => {
        if (!el) return;
        classes.forEach(pattern => {
            const oldClass = pattern.replace('{c}', prevTailwindColor);
            const newClass = pattern.replace('{c}', newColor);
            el.classList.remove(oldClass);
            el.classList.add(newClass);
        });
    });

    // Update theme selector dropdown to match
    const themeSelect = document.getElementById('themeSelect');
    if (themeSelect && themeSelect.value !== themeKey) {
        themeSelect.value = themeKey;
    }

    prevTailwindColor = newColor;
}

function onThemeChange() {
    const select = document.getElementById('themeSelect');
    if (select) {
        applyTheme(select.value);
    }
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    // Restore saved theme
    const savedTheme = localStorage.getItem('medadvice_theme') || 'medadvice';
    currentTheme = savedTheme;
    prevTailwindColor = (THEMES[savedTheme] || THEMES.medadvice).tailwindColor;
    applyTheme(savedTheme);

    // Check if user already has a session
    const savedSessionId = localStorage.getItem('medadvice_session_id');
    const savedDisclaimerAccepted = localStorage.getItem('medadvice_disclaimer_accepted');
    const savedPiiEnabled = localStorage.getItem('medadvice_pii_enabled');
    const savedToxicEnabled = localStorage.getItem('medadvice_toxic_enabled');
    const savedHallucinationEnabled = localStorage.getItem('medadvice_hallucination_enabled');
    const savedAiDefenseEnabled = localStorage.getItem('medadvice_ai_defense_enabled');
    const savedInternalPolicyEnabled = localStorage.getItem('medadvice_internal_policy_enabled');

    if (savedSessionId && savedDisclaimerAccepted === 'true') {
        sessionId = savedSessionId;
        disclaimerAccepted = true;
        piiEnabled = savedPiiEnabled === 'true';
        toxicEnabled = savedToxicEnabled === 'true';
        hallucinationEnabled = savedHallucinationEnabled === 'true';
        aiDefenseEnabled = savedAiDefenseEnabled === 'true';
        // Internal policy engine defaults ON unless explicitly turned off.
        internalPolicyEnabled = savedInternalPolicyEnabled !== 'false';
        showMainApp();
        
        // Set toggle state
        const toggle = document.getElementById('piiToggle');
        if (toggle) {
            toggle.checked = piiEnabled;
            updatePIIStatus();
        }
        
        const toxicToggle = document.getElementById('toxicToggle');
        if (toxicToggle) {
            toxicToggle.checked = toxicEnabled;
            updateToxicStatus();
        }
        
        const hallucinationToggle = document.getElementById('hallucinationToggle');
        if (hallucinationToggle) {
            hallucinationToggle.checked = hallucinationEnabled;
            updateHallucinationStatus();
        }

        const aiDefenseToggle = document.getElementById('aiDefenseToggle');
        if (aiDefenseToggle) {
            aiDefenseToggle.checked = aiDefenseEnabled;
            updateAIDefenseStatus();
        }

        const internalPolicyToggle = document.getElementById('internalPolicyToggle');
        if (internalPolicyToggle) {
            internalPolicyToggle.checked = internalPolicyEnabled;
            updateInternalPolicyStatus();
        }
        
        // Check auto-prompt status on load
        checkAutoPromptStatus();
    }
    
    // Add event listener to new session button as fallback
    const newSessionBtn = document.getElementById('newSessionBtn');
    if (newSessionBtn) {
        newSessionBtn.addEventListener('click', function(e) {
            console.log('New session button clicked via event listener');
        });
    }
});

function acceptDisclaimer() {
    disclaimerAccepted = true;
    localStorage.setItem('medadvice_disclaimer_accepted', 'true');
    createNewSession();
}

function declineDisclaimer() {
    alert('You must accept the disclaimer to use this service.');
    window.location.href = 'about:blank';
}

function showMainApp() {
    document.getElementById('disclaimerModal').classList.remove('active');
    document.getElementById('mainApp').classList.remove('hidden');
    document.getElementById('sessionId').textContent = sessionId;
    document.getElementById('messageInput').focus();
}

async function createNewSession() {
    try {
        const response = await fetch('/api/chat/session/new', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        });

        if (!response.ok) {
            throw new Error('Failed to create session');
        }

        const data = await response.json();
        sessionId = data.session_id;
        localStorage.setItem('medadvice_session_id', sessionId);

        showMainApp();
    } catch (error) {
        console.error('Error creating session:', error);
        alert('Failed to create session. Please try again.');
    }
}

function handleKeyPress(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
    }
}

async function sendMessage() {
    const input = document.getElementById('messageInput');
    const message = input.value.trim();

    if (!message) {
        return;
    }

    input.disabled = true;
    document.getElementById('sendButton').disabled = true;
    document.getElementById('loadingIndicator').classList.remove('hidden');

    addMessageToChat('user', message, 'user_message');

    input.value = '';

    try {
        const response = await fetch('/api/chat/message', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                session_id: sessionId,
                message: message,
                disclaimer_accepted: disclaimerAccepted,
                theme: currentTheme,
                force_pii_injection: piiEnabled,
                force_toxic_injection: toxicEnabled,
                force_hallucination_injection: hallucinationEnabled,
                ai_defense_review: aiDefenseEnabled,
                internal_policy_review: internalPolicyEnabled
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to send message');
        }

        const data = await response.json();

        addMessageToChat('assistant', data.message, data.type, data.severity, data.escalated);

        if (data.escalated) {
            showEscalationWarning();
        }

    } catch (error) {
        console.error('Error sending message:', error);
        const theme = THEMES[currentTheme] || THEMES.medadvice;
        addMessageToChat('assistant', theme.errorFallback, 'safety_warning');
    } finally {
        input.disabled = false;
        document.getElementById('sendButton').disabled = false;
        document.getElementById('loadingIndicator').classList.add('hidden');
        input.focus();
    }
}

function addMessageToChat(role, content, type, severity = null, escalated = false) {
    const chatContainer = document.getElementById('chatContainer');

    const welcomeMsg = chatContainer.querySelector('.text-center.text-gray-500');
    if (welcomeMsg) {
        welcomeMsg.remove();
    }

    const messageDiv = document.createElement('div');
    messageDiv.className = 'p-4 rounded-lg';

    if (role === 'user') {
        messageDiv.classList.add('message-user', 'ml-12', 'text-right');
    } else {
        if (type === 'clarifying_question') {
            messageDiv.classList.add('message-clarifying', 'mr-12');
        } else if (type === 'recommendation') {
            messageDiv.classList.add('message-recommendation', 'mr-12');
        } else if (type === 'safety_warning') {
            messageDiv.classList.add('message-warning', 'mr-12');
        } else if (type === 'escalation') {
            messageDiv.classList.add('message-escalation', 'mr-12');
        } else {
            messageDiv.classList.add('message-assistant', 'mr-12');
        }
    }

    let severityBadge = '';
    if (severity) {
        const severityColors = {
            'LOW': 'bg-green-100 text-green-800',
            'MEDIUM': 'bg-yellow-100 text-yellow-800',
            'HIGH': 'bg-orange-100 text-orange-800',
            'EMERGENCY': 'bg-red-100 text-red-800'
        };
        const colorClass = severityColors[severity] || 'bg-gray-100 text-gray-800';
        severityBadge = `<span class="inline-block px-2 py-1 text-xs font-semibold rounded ${colorClass} mb-2">${severity}</span><br>`;
    }

    let escalationBadge = '';
    if (escalated) {
        escalationBadge = `<span class="inline-block px-2 py-1 text-xs font-semibold rounded bg-red-100 text-red-800 mb-2">ESCALATED FOR REVIEW</span><br>`;
    }

    const formattedContent = formatContent(content);

    messageDiv.innerHTML = `
        ${severityBadge}
        ${escalationBadge}
        <div class="text-sm">${formattedContent}</div>
        <div class="text-xs mt-2 opacity-70">${new Date().toLocaleTimeString()}</div>
    `;

    chatContainer.appendChild(messageDiv);
    chatContainer.scrollTop = chatContainer.scrollHeight;
}

function formatContent(content) {
    let formatted = content;
    formatted = formatted.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    formatted = formatted.replace(/^• (.+)$/gm, '<li>$1</li>');
    formatted = formatted.replace(/(<li>.*<\/li>\s*)+/g, '<ul class="list-disc list-inside my-2">$&</ul>');
    formatted = formatted.replace(/\n/g, '<br>');
    return formatted;
}

function showEscalationWarning() {
    const warning = document.createElement('div');
    warning.className = 'bg-orange-100 border-l-4 border-orange-500 p-4 mb-4 rounded';
    warning.innerHTML = `
        <p class="font-bold text-orange-700">This consultation has been escalated for human review</p>
        <p class="text-orange-600 text-sm">A professional will review this case. Please seek immediate help if your situation is urgent.</p>
    `;

    const container = document.querySelector('.container');
    container.insertBefore(warning, container.children[2]);

    setTimeout(() => warning.remove(), 10000);
}

function startNewSession() {
    console.log('startNewSession called');
    
    if (!confirm('Are you sure you want to start a new session? This will clear your current conversation.')) {
        return;
    }
    
    const chatContainer = document.getElementById('chatContainer');
    const theme = THEMES[currentTheme] || THEMES.medadvice;
    chatContainer.innerHTML = `
        <div class="text-center text-gray-500 py-8">
            <div class="spinner mx-auto mb-4"></div>
            <p>Starting new session...</p>
        </div>
    `;
    
    localStorage.removeItem('medadvice_session_id');
    
    fetch('/api/chat/session/new', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        }
    })
    .then(response => {
        if (!response.ok) {
            throw new Error('Failed to create new session');
        }
        return response.json();
    })
    .then(data => {
        sessionId = data.session_id;
        localStorage.setItem('medadvice_session_id', sessionId);
        document.getElementById('sessionId').textContent = sessionId;
        
        chatContainer.innerHTML = `
            <div class="text-center text-gray-500 py-8">
                <p>${theme.welcomeGreeting}</p>
                <p class="text-sm mt-2">${theme.welcomeSubtext}</p>
            </div>
        `;
        
        document.getElementById('messageInput').focus();
        console.log('New session created:', sessionId);
    })
    .catch(error => {
        console.error('Error creating new session:', error);
        alert('Failed to create new session. Please refresh the page and try again.');
        
        chatContainer.innerHTML = `
            <div class="text-center text-red-500 py-8">
                <p>Failed to create new session</p>
                <p class="text-sm mt-2">Please refresh the page and try again.</p>
            </div>
        `;
    });
}

function clearSession() {
    startNewSession();
}

function togglePII() {
    const toggle = document.getElementById('piiToggle');
    piiEnabled = toggle.checked;
    localStorage.setItem('medadvice_pii_enabled', piiEnabled);
    updatePIIStatus();
    console.log('PII injection', piiEnabled ? 'enabled' : 'disabled');
}

function updatePIIStatus() {
    const statusElement = document.getElementById('piiStatus');
    if (piiEnabled) {
        statusElement.textContent = 'ALWAYS ON';
        statusElement.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-green-100 text-green-600';
    } else {
        statusElement.textContent = 'RANDOM (25%)';
        statusElement.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-gray-100 text-gray-600';
    }
}

function toggleToxic() {
    const toggle = document.getElementById('toxicToggle');
    toxicEnabled = toggle.checked;
    localStorage.setItem('medadvice_toxic_enabled', toxicEnabled);
    updateToxicStatus();
    console.log('Toxic injection', toxicEnabled ? 'enabled' : 'disabled');
}

function updateToxicStatus() {
    const statusElement = document.getElementById('toxicStatus');
    if (toxicEnabled) {
        statusElement.textContent = 'ALWAYS ON';
        statusElement.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-red-100 text-red-600';
    } else {
        statusElement.textContent = 'RANDOM (25%)';
        statusElement.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-gray-100 text-gray-600';
    }
}

function toggleHallucination() {
    const toggle = document.getElementById('hallucinationToggle');
    hallucinationEnabled = toggle.checked;
    localStorage.setItem('medadvice_hallucination_enabled', hallucinationEnabled);
    updateHallucinationStatus();
    console.log('Hallucination injection', hallucinationEnabled ? 'enabled' : 'disabled');
}

function updateHallucinationStatus() {
    const statusElement = document.getElementById('hallucinationStatus');
    if (hallucinationEnabled) {
        statusElement.textContent = 'ALWAYS ON';
        statusElement.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-purple-100 text-purple-600';
    } else {
        statusElement.textContent = 'RANDOM (25%)';
        statusElement.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-gray-100 text-gray-600';
    }
}

function toggleAIDefense() {
    const toggle = document.getElementById('aiDefenseToggle');
    aiDefenseEnabled = toggle.checked;
    localStorage.setItem('medadvice_ai_defense_enabled', aiDefenseEnabled);
    updateAIDefenseStatus();
    console.log('Cisco AI Defense policy review', aiDefenseEnabled ? 'enabled' : 'disabled');
}

function updateAIDefenseStatus() {
    const statusElement = document.getElementById('aiDefenseStatus');
    if (!statusElement) return;
    if (aiDefenseEnabled) {
        statusElement.textContent = 'REVIEWING';
        statusElement.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-sky-100 text-sky-700';
    } else {
        statusElement.textContent = 'OFF';
        statusElement.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-gray-100 text-gray-600';
    }
}

function toggleInternalPolicy() {
    const toggle = document.getElementById('internalPolicyToggle');
    internalPolicyEnabled = toggle.checked;
    localStorage.setItem('medadvice_internal_policy_enabled', internalPolicyEnabled);
    updateInternalPolicyStatus();
    console.log('Internal policy engine', internalPolicyEnabled ? 'enabled' : 'disabled');
}

function updateInternalPolicyStatus() {
    const statusElement = document.getElementById('internalPolicyStatus');
    if (!statusElement) return;
    if (internalPolicyEnabled) {
        statusElement.textContent = 'ON';
        statusElement.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-slate-200 text-slate-700';
    } else {
        statusElement.textContent = 'OFF';
        statusElement.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-gray-100 text-gray-600';
    }
}

async function toggleAutoPrompt() {
    const toggle = document.getElementById('autoPromptToggle');
    const newState = toggle.checked;
    
    try {
        const endpoint = newState ? '/api/chat/auto-prompt/start' : '/api/chat/auto-prompt/stop';
        const response = await fetch(endpoint, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        });
        
        if (!response.ok) {
            throw new Error('Failed to toggle auto-prompt');
        }
        
        const data = await response.json();
        autoPromptEnabled = data.running;
        updateAutoPromptStatus(data);
        
        if (autoPromptEnabled) {
            startAutoPromptStatusPolling();
        } else {
            stopAutoPromptStatusPolling();
        }
        
        console.log('Auto-prompt', autoPromptEnabled ? 'enabled' : 'disabled', data);
    } catch (error) {
        console.error('Error toggling auto-prompt:', error);
        toggle.checked = !newState;
        alert('Failed to toggle auto-prompt. Please try again.');
    }
}

async function checkAutoPromptStatus() {
    try {
        const response = await fetch('/api/chat/auto-prompt/status');
        if (response.ok) {
            const data = await response.json();
            autoPromptEnabled = data.running;
            
            const toggle = document.getElementById('autoPromptToggle');
            if (toggle) {
                toggle.checked = autoPromptEnabled;
            }
            
            updateAutoPromptStatus(data);
            
            if (autoPromptEnabled) {
                startAutoPromptStatusPolling();
            }
        }
    } catch (error) {
        console.error('Error checking auto-prompt status:', error);
    }
}

function updateAutoPromptStatus(data) {
    const statusElement = document.getElementById('autoPromptStatus');
    const statsElement = document.getElementById('autoPromptStats');
    const countElement = document.getElementById('autoPromptCount');
    
    if (data.running) {
        statusElement.textContent = 'RUNNING';
        statusElement.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-indigo-100 text-indigo-600 animate-pulse';
        statsElement.classList.remove('hidden');
        countElement.textContent = data.sessions_created || 0;
    } else {
        statusElement.textContent = 'OFF';
        statusElement.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-gray-100 text-gray-600';
        if (data.sessions_created > 0) {
            statsElement.classList.remove('hidden');
            countElement.textContent = data.sessions_created;
        } else {
            statsElement.classList.add('hidden');
        }
    }
}

function startAutoPromptStatusPolling() {
    if (autoPromptStatusInterval) {
        clearInterval(autoPromptStatusInterval);
    }
    
    autoPromptStatusInterval = setInterval(async () => {
        try {
            const response = await fetch('/api/chat/auto-prompt/status');
            if (response.ok) {
                const data = await response.json();
                updateAutoPromptStatus(data);
                
                if (!data.running) {
                    const toggle = document.getElementById('autoPromptToggle');
                    if (toggle) {
                        toggle.checked = false;
                    }
                    stopAutoPromptStatusPolling();
                }
            }
        } catch (error) {
            console.error('Error polling auto-prompt status:', error);
        }
    }, 10000);
}

function stopAutoPromptStatusPolling() {
    if (autoPromptStatusInterval) {
        clearInterval(autoPromptStatusInterval);
        autoPromptStatusInterval = null;
    }
}

function toggleSettings() {
    const panel = document.getElementById('settingsPanel');
    const arrow = document.getElementById('settingsArrow');
    const button = arrow ? arrow.closest('button') : null;
    if (!panel) return;

    const willExpand = panel.classList.contains('hidden');
    panel.classList.toggle('hidden');

    if (arrow) {
        arrow.classList.toggle('-rotate-90', !willExpand);
    }
    if (button) {
        button.setAttribute('aria-expanded', willExpand ? 'true' : 'false');
    }
}
