// Settings page: local log directory + Splunk HEC destinations.

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function attr(s) {
  return esc(s).replace(/"/g, '&quot;');
}
async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  let data = null;
  try { data = await r.json(); } catch (_) {}
  if (!r.ok) {
    const detail = data && (data.detail || JSON.stringify(data));
    throw new Error(detail || ('HTTP ' + r.status));
  }
  return data;
}

// ---------------------------------------------------------------- log dir
async function loadLogsDir() {
  try {
    const d = await api('GET', '/api/settings');
    document.getElementById('logsDir').value = d.logs_directory || '';
  } catch (e) { setLogsStatus('Failed to load: ' + e.message, false); }
}
async function saveLogsDir() {
  const path = document.getElementById('logsDir').value.trim();
  try {
    const d = await api('PUT', '/api/settings', { logs_directory: path || 'logs' });
    document.getElementById('logsDir').value = d.logs_directory;
    setLogsStatus('Saved — new governance logs will write to "' + d.logs_directory + '".', true);
  } catch (e) { setLogsStatus('Error: ' + e.message, false); }
}
function setLogsStatus(msg, ok) {
  const el = document.getElementById('logsStatus');
  el.textContent = msg;
  el.className = 'text-sm mt-2 ' + (ok ? 'text-green-600' : 'text-red-600');
}

// ------------------------------------------------------------ destinations
async function loadDestinations() {
  try {
    const d = await api('GET', '/api/hec/destinations');
    renderDestinations(d.destinations || []);
  } catch (e) {
    document.getElementById('destinations').innerHTML =
      `<p class="text-red-600 text-sm">Failed to load destinations: ${esc(e.message)}</p>`;
  }
}
function renderDestinations(dests) {
  const box = document.getElementById('destinations');
  if (!dests.length) {
    box.innerHTML = `<p class="text-gray-400 text-sm">No HEC destinations yet. Click “+ Add destination”.</p>`;
    return;
  }
  box.innerHTML = dests.map(cardHTML).join('');
}
function field(label, f, value, type) {
  return `<div>
    <label class="block text-xs font-semibold text-gray-600 mb-1">${label}</label>
    <input data-field="${f}" type="${type || 'text'}" value="${attr(value)}"
      class="w-full p-2 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"></div>`;
}
function num(label, f, value) {
  return `<div>
    <label class="block text-xs font-semibold text-gray-600 mb-1">${label}</label>
    <input data-field="${f}" type="number" value="${attr(value)}"
      class="w-full p-2 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"></div>`;
}
function cardHTML(d) {
  const id = d.id;
  const tokenPh = d.token_present
    ? `•••• ${esc(d.token_last4)} (saved — leave blank to keep)`
    : 'paste HEC token';
  return `<div class="border border-gray-200 rounded-lg p-4" data-dest="${attr(id)}">
    <div class="flex items-center justify-between mb-3 gap-3">
      <input data-field="name" value="${attr(d.name || '')}"
        class="font-semibold text-gray-800 text-lg flex-1 p-1 border border-transparent hover:border-gray-200 rounded focus:outline-none focus:ring-2 focus:ring-blue-500">
      <label class="flex items-center gap-2 text-sm text-gray-700 whitespace-nowrap">
        <input data-field="enabled" type="checkbox" ${d.enabled ? 'checked' : ''}> Enabled</label>
    </div>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">
      ${field('HEC URL (https://host:8088)', 'url', d.url || '')}
      <div>
        <label class="block text-xs font-semibold text-gray-600 mb-1">HEC token</label>
        <input data-field="token" type="password" placeholder="${attr(tokenPh)}"
          class="w-full p-2 border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"></div>
      ${field('Index', 'index', d.index || '')}
      ${field('Sourcetype', 'sourcetype', d.sourcetype || '')}
      ${field('Source', 'source', d.source || '')}
      ${field('Host', 'host', d.host || '')}
    </div>
    <label class="flex items-center gap-2 text-sm text-gray-700 mb-3">
      <input data-field="verify_tls" type="checkbox" ${d.verify_tls ? 'checked' : ''}> Verify TLS certificate</label>
    <details class="mb-3">
      <summary class="text-sm text-gray-600 cursor-pointer">Performance</summary>
      <div class="grid grid-cols-2 md:grid-cols-5 gap-3 mt-2">
        ${num('Batch size', 'batch_size', d.batch_size)}
        ${num('Flush (s)', 'flush_interval_s', d.flush_interval_s)}
        ${num('Queue max', 'queue_max', d.queue_max)}
        ${num('Timeout (s)', 'request_timeout_s', d.request_timeout_s)}
        ${num('Max retries', 'max_retries', d.max_retries)}
      </div>
    </details>
    <div class="flex items-center gap-2">
      <button onclick="saveDestination('${attr(id)}')" class="px-3 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700">Save</button>
      <button onclick="testDestination('${attr(id)}')" class="px-3 py-1.5 bg-slate-600 text-white rounded text-sm hover:bg-slate-700">Test connection</button>
      <button onclick="deleteDestination('${attr(id)}')" class="px-3 py-1.5 bg-red-100 text-red-700 rounded text-sm hover:bg-red-200">Delete</button>
      <span data-result class="text-sm ml-2"></span>
    </div>
    <div data-stats class="text-xs text-gray-500 mt-3 font-mono"></div>
  </div>`;
}
function cardFor(id) { return document.querySelector(`[data-dest="${CSS.escape(id)}"]`); }
function collectDest(card) {
  const get = (f) => card.querySelector(`[data-field="${f}"]`);
  const patch = {
    name: get('name').value.trim(),
    enabled: get('enabled').checked,
    url: get('url').value.trim(),
    verify_tls: get('verify_tls').checked,
    index: get('index').value.trim(),
    source: get('source').value.trim(),
    sourcetype: get('sourcetype').value.trim(),
    host: get('host').value.trim(),
  };
  const tok = get('token').value.trim();
  if (tok) patch.token = tok;
  for (const [f, kind] of Object.entries({ batch_size: 'i', flush_interval_s: 'f', queue_max: 'i', request_timeout_s: 'f', max_retries: 'i' })) {
    const v = kind === 'i' ? parseInt(get(f).value, 10) : parseFloat(get(f).value);
    if (!Number.isNaN(v)) patch[f] = v;
  }
  return patch;
}
function setResult(id, msg, ok) {
  const el = cardFor(id).querySelector('[data-result]');
  el.textContent = msg;
  el.className = 'text-sm ml-2 ' + (ok ? 'text-green-600' : 'text-red-600');
}
async function saveDestination(id) {
  try {
    await api('PUT', '/api/hec/destinations/' + encodeURIComponent(id), collectDest(cardFor(id)));
    setResult(id, 'Saved.', true);
    await loadDestinations();
  } catch (e) { setResult(id, 'Error: ' + e.message, false); }
}
async function testDestination(id) {
  setResult(id, 'Saving + testing…', true);
  try {
    await api('PUT', '/api/hec/destinations/' + encodeURIComponent(id), collectDest(cardFor(id)));
    const r = await api('POST', '/api/hec/destinations/' + encodeURIComponent(id) + '/test');
    if (r.ok) setResult(id, `✓ Success (${r.status_code}, ${r.latency_ms} ms)`, true);
    else setResult(id, '✗ ' + (r.error || 'failed'), false);
    await loadDestinations();
  } catch (e) { setResult(id, 'Error: ' + e.message, false); }
}
async function deleteDestination(id) {
  if (!confirm('Delete this HEC destination?')) return;
  try { await api('DELETE', '/api/hec/destinations/' + encodeURIComponent(id)); await loadDestinations(); }
  catch (e) { setResult(id, 'Error: ' + e.message, false); }
}
async function addDestination() {
  try { await api('POST', '/api/hec/destinations', { name: 'New destination' }); await loadDestinations(); }
  catch (e) { alert('Could not add destination: ' + e.message); }
}

// -------------------------------------------------------------------- stats
async function loadStats() {
  let data;
  try { data = await api('GET', '/api/hec/stats'); } catch (_) { return; }
  for (const s of (data.destinations || [])) {
    const card = cardFor(s.id);
    if (!card) continue;
    const el = card.querySelector('[data-stats]');
    const state = s.running ? 'running' : (s.enabled ? 'enabled (idle)' : 'disabled');
    let line = `sent ${s.events_sent} · failed ${s.events_failed} · dropped ${s.events_dropped} · queue ${s.queue_depth}/${s.queue_capacity} · ${state}`;
    if (s.last_latency_ms != null) line += ` · ${s.last_latency_ms} ms`;
    if (s.last_error) line += `  —  last error: ${esc(s.last_error)}`;
    el.textContent = line;
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  await loadLogsDir();
  await loadDestinations();
  await loadStats();
  setInterval(() => { if (!document.hidden) loadStats(); }, 2500);
});
