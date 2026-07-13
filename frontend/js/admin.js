let severityChart = null;
let tokenChart = null;

// HTML/attribute escaping for server-provided values rendered via innerHTML.
// Escalation reasons and session/operation ids can carry attendee-influenced
// content, so escape before interpolating (text context) or embedding in an
// onclick attribute (attribute context).
function escHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function escAttr(s) {
    return escHtml(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Load data on page load
document.addEventListener('DOMContentLoaded', function() {
    loadMetrics();
    loadEscalations();
    loadRecentInteractions();

    // Refresh every 30 seconds
    setInterval(() => {
        loadMetrics();
        loadEscalations();
        loadRecentInteractions();
    }, 30000);
});

async function loadMetrics() {
    try {
        const response = await fetch('/admin/logs/metrics?days=7');
        if (!response.ok) throw new Error('Failed to load metrics');

        const data = await response.json();

        // Update metric cards
        document.getElementById('totalInteractions').textContent = data.total_interactions.toLocaleString();
        document.getElementById('escalationCount').textContent = data.escalation_count.toLocaleString();
        document.getElementById('escalationRate').textContent = `${data.escalation_rate}% escalation rate`;
        document.getElementById('avgLatency').textContent = `${data.average_latency.toFixed(2)}s`;
        document.getElementById('totalTokens').textContent = data.total_tokens.toLocaleString();
        document.getElementById('piiCount').textContent = data.pii_detection_count.toLocaleString();
        document.getElementById('guardrailCount').textContent = data.guardrail_trigger_count.toLocaleString();

        // Update charts
        updateSeverityChart(data.severity_distribution);
        updateTokenChart(data.total_input_tokens, data.total_output_tokens);

    } catch (error) {
        console.error('Error loading metrics:', error);
    }
}

function updateSeverityChart(distribution) {
    const ctx = document.getElementById('severityChart').getContext('2d');

    const data = {
        labels: Object.keys(distribution),
        datasets: [{
            data: Object.values(distribution),
            backgroundColor: [
                '#10b981', // GREEN for LOW
                '#fbbf24', // YELLOW for MEDIUM
                '#f97316', // ORANGE for HIGH
                '#ef4444'  // RED for EMERGENCY
            ]
        }]
    };

    if (severityChart) {
        severityChart.data = data;
        severityChart.update();
    } else {
        severityChart = new Chart(ctx, {
            type: 'doughnut',
            data: data,
            options: {
                responsive: true,
                plugins: {
                    legend: {
                        position: 'bottom'
                    }
                }
            }
        });
    }
}

function updateTokenChart(inputTokens, outputTokens) {
    const ctx = document.getElementById('tokenChart').getContext('2d');

    const data = {
        labels: ['Input Tokens', 'Output Tokens'],
        datasets: [{
            data: [inputTokens, outputTokens],
            backgroundColor: ['#7c3aed', '#c4b5fd']
        }]
    };

    if (tokenChart) {
        tokenChart.data = data;
        tokenChart.update();
    } else {
        tokenChart = new Chart(ctx, {
            type: 'bar',
            data: data,
            options: {
                responsive: true,
                plugins: {
                    legend: {
                        display: false
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true
                    }
                }
            }
        });
    }
}

async function loadEscalations() {
    const filter = document.querySelector('input[name="escalationFilter"]:checked').value;
    const status = filter === 'all' ? null : filter;

    try {
        let url = '/admin/logs/escalations?limit=50';
        if (status) {
            url += `&status=${status}`;
        }

        const response = await fetch(url);
        if (!response.ok) throw new Error('Failed to load escalations');

        const data = await response.json();
        const tbody = document.getElementById('escalationTableBody');

        if (data.escalations.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center py-4 text-gray-500">No escalations found</td></tr>';
            return;
        }

        tbody.innerHTML = data.escalations.map(esc => {
            const statusColors = {
                'pending': 'bg-yellow-100 text-yellow-800',
                'reviewed': 'bg-violet-100 text-violet-800',
                'resolved': 'bg-green-100 text-green-800'
            };

            const severityColors = {
                'LOW': 'text-green-600',
                'MEDIUM': 'text-yellow-600',
                'HIGH': 'text-orange-600',
                'EMERGENCY': 'text-red-600 font-bold'
            };

            return `
                <tr class="border-b hover:bg-gray-50">
                    <td class="px-4 py-2 text-sm">${new Date(esc.timestamp).toLocaleString()}</td>
                    <td class="px-4 py-2 text-sm font-mono">${escHtml(String(esc.session_id).substring(0, 8))}...</td>
                    <td class="px-4 py-2 text-sm ${severityColors[esc.severity] || ''}">${escHtml(esc.severity)}</td>
                    <td class="px-4 py-2 text-sm">${escHtml(String(esc.reason).substring(0, 50))}...</td>
                    <td class="px-4 py-2">
                        <span class="px-2 py-1 text-xs rounded ${statusColors[esc.review_status] || ''}">${escHtml(esc.review_status)}</span>
                    </td>
                    <td class="px-4 py-2">
                        <button onclick="viewEscalation('${escAttr(esc.session_id)}')" class="text-violet-600 hover:underline text-sm">View</button>
                        ${esc.review_status === 'pending' ? `
                        <button onclick="reviewEscalation('${escAttr(esc.escalation_id)}')" class="text-green-600 hover:underline text-sm ml-2">Review</button>
                        ` : ''}
                    </td>
                </tr>
            `;
        }).join('');

    } catch (error) {
        console.error('Error loading escalations:', error);
    }
}

async function loadRecentInteractions() {
    try {
        const response = await fetch('/admin/logs/interactions?limit=20');
        if (!response.ok) throw new Error('Failed to load interactions');

        const data = await response.json();
        const tbody = document.getElementById('interactionsTableBody');

        if (data.logs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center py-4 text-gray-500">No interactions found</td></tr>';
            return;
        }

        tbody.innerHTML = data.logs.map(log => {
            const flags = [];
            if (log.pii_detected) flags.push('<span class="px-2 py-1 text-xs bg-red-100 text-red-800 rounded">PII</span>');
            if (log.safety_violated) flags.push('<span class="px-2 py-1 text-xs bg-orange-100 text-orange-800 rounded">Safety</span>');
            if (log.guardrail_triggered) flags.push('<span class="px-2 py-1 text-xs bg-yellow-100 text-yellow-800 rounded">Guardrail</span>');
            if (log.error_type) flags.push('<span class="px-2 py-1 text-xs bg-red-100 text-red-800 rounded">Error</span>');

            return `
                <tr class="border-b hover:bg-gray-50">
                    <td class="px-4 py-2 text-sm">${new Date(log.timestamp).toLocaleString()}</td>
                    <td class="px-4 py-2 text-sm font-mono">${escHtml(String(log.session_id).substring(0, 8))}...</td>
                    <td class="px-4 py-2 text-sm">${escHtml(log.operation_name)}</td>
                    <td class="px-4 py-2 text-sm">${log.usage_total_tokens || '-'}</td>
                    <td class="px-4 py-2">${flags.join(' ') || '-'}</td>
                    <td class="px-4 py-2">
                        <button onclick="viewSession('${escAttr(log.session_id)}')" class="text-violet-600 hover:underline text-sm">View</button>
                    </td>
                </tr>
            `;
        }).join('');

    } catch (error) {
        console.error('Error loading interactions:', error);
    }
}

async function viewSession(sessionId) {
    window.location.href = `/governance-ui?session=${sessionId}`;
}

async function viewEscalation(sessionId) {
    window.location.href = `/governance-ui?session=${sessionId}`;
}

async function reviewEscalation(escalationId) {
    const reviewerId = prompt('Enter your reviewer ID:');
    if (!reviewerId) return;

    const notes = prompt('Enter review notes:');
    if (!notes) return;

    const status = confirm('Mark as resolved? (Cancel for "reviewed" status)') ? 'resolved' : 'reviewed';

    try {
        const response = await fetch(`/admin/escalations/${escalationId}/review?new_status=${status}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                reviewer_id: reviewerId,
                review_notes: notes
            })
        });

        if (!response.ok) throw new Error('Failed to update escalation');

        alert('Escalation updated successfully');
        loadEscalations();

    } catch (error) {
        console.error('Error updating escalation:', error);
        alert('Failed to update escalation');
    }
}

async function exportLogs(logType) {
    try {
        const response = await fetch(`/admin/logs/export?log_type=${logType}&format=json`);
        if (!response.ok) throw new Error('Failed to export logs');

        const data = await response.json();
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${logType}_export_${new Date().toISOString().split('T')[0]}.json`;
        a.click();
        URL.revokeObjectURL(url);

    } catch (error) {
        console.error('Error exporting logs:', error);
        alert('Failed to export logs');
    }
}
