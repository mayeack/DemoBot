let sessionId = null;
let disclaimerAccepted = false;
let piiEnabled = false;
let toxicEnabled = false;
let hallucinationEnabled = false;
let autoPromptEnabled = false;
let autoPromptStatusInterval = null;

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    // Check if user already has a session
    const savedSessionId = localStorage.getItem('medadvice_session_id');
    const savedDisclaimerAccepted = localStorage.getItem('medadvice_disclaimer_accepted');
    const savedPiiEnabled = localStorage.getItem('medadvice_pii_enabled');
    const savedToxicEnabled = localStorage.getItem('medadvice_toxic_enabled');
    const savedHallucinationEnabled = localStorage.getItem('medadvice_hallucination_enabled');

    if (savedSessionId && savedDisclaimerAccepted === 'true') {
        sessionId = savedSessionId;
        disclaimerAccepted = true;
        piiEnabled = savedPiiEnabled === 'true';
        toxicEnabled = savedToxicEnabled === 'true';
        hallucinationEnabled = savedHallucinationEnabled === 'true';
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
        
        // Check auto-prompt status on load
        checkAutoPromptStatus();
    }
    
    // Add event listener to new session button as fallback
    const newSessionBtn = document.getElementById('newSessionBtn');
    if (newSessionBtn) {
        newSessionBtn.addEventListener('click', function(e) {
            // Prevent default if the onclick handler didn't fire
            console.log('New session button clicked via event listener');
        });
    }
});

function acceptDisclaimer() {
    disclaimerAccepted = true;
    localStorage.setItem('medadvice_disclaimer_accepted', 'true');

    // Create new session
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

    // Disable input while processing
    input.disabled = true;
    document.getElementById('sendButton').disabled = true;
    document.getElementById('loadingIndicator').classList.remove('hidden');

    // Add user message to chat
    addMessageToChat('user', message, 'user_message');

    // Clear input
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
                force_pii_injection: piiEnabled,
                force_toxic_injection: toxicEnabled,
                force_hallucination_injection: hallucinationEnabled
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to send message');
        }

        const data = await response.json();

        // Add assistant response to chat
        addMessageToChat('assistant', data.message, data.type, data.severity, data.escalated);

        // If escalated, show warning
        if (data.escalated) {
            showEscalationWarning();
        }

    } catch (error) {
        console.error('Error sending message:', error);
        addMessageToChat('assistant', 'Sorry, I encountered an error. Please try again or seek immediate medical care if urgent.', 'safety_warning');
    } finally {
        // Re-enable input
        input.disabled = false;
        document.getElementById('sendButton').disabled = false;
        document.getElementById('loadingIndicator').classList.add('hidden');
        input.focus();
    }
}

function addMessageToChat(role, content, type, severity = null, escalated = false) {
    const chatContainer = document.getElementById('chatContainer');

    // Remove welcome message if present
    const welcomeMsg = chatContainer.querySelector('.text-center.text-gray-500');
    if (welcomeMsg) {
        welcomeMsg.remove();
    }

    const messageDiv = document.createElement('div');
    messageDiv.className = 'p-4 rounded-lg';

    // Style based on message type
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

    // Add severity badge if present
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

    // Add escalation badge if escalated
    let escalationBadge = '';
    if (escalated) {
        escalationBadge = `<span class="inline-block px-2 py-1 text-xs font-semibold rounded bg-red-100 text-red-800 mb-2">⚠️ ESCALATED FOR REVIEW</span><br>`;
    }

    // Format content (convert markdown-style formatting)
    const formattedContent = formatContent(content);

    messageDiv.innerHTML = `
        ${severityBadge}
        ${escalationBadge}
        <div class="text-sm">${formattedContent}</div>
        <div class="text-xs mt-2 opacity-70">${new Date().toLocaleTimeString()}</div>
    `;

    chatContainer.appendChild(messageDiv);

    // Scroll to bottom
    chatContainer.scrollTop = chatContainer.scrollHeight;
}

function formatContent(content) {
    // Simple markdown-style formatting
    let formatted = content;

    // Bold
    formatted = formatted.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

    // Bullet points
    formatted = formatted.replace(/^• (.+)$/gm, '<li>$1</li>');
    formatted = formatted.replace(/(<li>.*<\/li>\s*)+/g, '<ul class="list-disc list-inside my-2">$&</ul>');

    // Line breaks
    formatted = formatted.replace(/\n/g, '<br>');

    return formatted;
}

function showEscalationWarning() {
    const warning = document.createElement('div');
    warning.className = 'bg-orange-100 border-l-4 border-orange-500 p-4 mb-4 rounded';
    warning.innerHTML = `
        <p class="font-bold text-orange-700">⚠️ This consultation has been escalated for human review</p>
        <p class="text-orange-600 text-sm">A medical professional will review this case. Please seek immediate care if symptoms are urgent.</p>
    `;

    const container = document.querySelector('.container');
    container.insertBefore(warning, container.children[2]);

    // Auto-remove after 10 seconds
    setTimeout(() => warning.remove(), 10000);
}

// Start new session function
function startNewSession() {
    console.log('startNewSession called'); // Debug log
    
    if (!confirm('Are you sure you want to start a new session? This will clear your current conversation.')) {
        return;
    }
    
    // Show loading state
    const chatContainer = document.getElementById('chatContainer');
    chatContainer.innerHTML = `
        <div class="text-center text-gray-500 py-8">
            <div class="spinner mx-auto mb-4"></div>
            <p>Starting new session...</p>
        </div>
    `;
    
    // Clear current session from localStorage
    localStorage.removeItem('medadvice_session_id');
    
    // Create new session via API
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
        // Update session ID
        sessionId = data.session_id;
        localStorage.setItem('medadvice_session_id', sessionId);
        
        // Update session ID display
        document.getElementById('sessionId').textContent = sessionId;
        
        // Show welcome message
        chatContainer.innerHTML = `
            <div class="text-center text-gray-500 py-8">
                <p>👋 Welcome! How can I help you today?</p>
                <p class="text-sm mt-2">Please describe your symptoms or health concern.</p>
            </div>
        `;
        
        // Focus on input
        document.getElementById('messageInput').focus();
        
        console.log('New session created:', sessionId);
    })
    .catch(error => {
        console.error('Error creating new session:', error);
        alert('Failed to create new session. Please refresh the page and try again.');
        
        // Restore error state
        chatContainer.innerHTML = `
            <div class="text-center text-red-500 py-8">
                <p>❌ Failed to create new session</p>
                <p class="text-sm mt-2">Please refresh the page and try again.</p>
            </div>
        `;
    });
}

// Clear session function (legacy - calls startNewSession)
function clearSession() {
    startNewSession();
}

// Toggle PII/PHI injection
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

// Toggle toxic content injection
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

// Toggle hallucination injection
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

// Auto-prompt functions
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
        
        // Start or stop status polling
        if (autoPromptEnabled) {
            startAutoPromptStatusPolling();
        } else {
            stopAutoPromptStatusPolling();
        }
        
        console.log('Auto-prompt', autoPromptEnabled ? 'enabled' : 'disabled', data);
    } catch (error) {
        console.error('Error toggling auto-prompt:', error);
        // Revert toggle state on error
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
            
            // Update toggle state
            const toggle = document.getElementById('autoPromptToggle');
            if (toggle) {
                toggle.checked = autoPromptEnabled;
            }
            
            updateAutoPromptStatus(data);
            
            // Start polling if already running
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
    // Poll status every 10 seconds to update session count
    if (autoPromptStatusInterval) {
        clearInterval(autoPromptStatusInterval);
    }
    
    autoPromptStatusInterval = setInterval(async () => {
        try {
            const response = await fetch('/api/chat/auto-prompt/status');
            if (response.ok) {
                const data = await response.json();
                updateAutoPromptStatus(data);
                
                // Stop polling if auto-prompt was stopped externally
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
