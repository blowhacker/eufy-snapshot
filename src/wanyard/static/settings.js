const fmt = {
  bytes: b => b > 1e9 ? (b/1e9).toFixed(1)+'GB' : b > 1e6 ? (b/1e6).toFixed(0)+'MB' : (b/1e3).toFixed(0)+'KB',
  ts:    t => t ? new Date(t*1000).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}) : '--',
};

// ── Status (system KPIs + pipeline chip) ─────────────
let _lastThreads = {};
let _sources = [];
let _notificationRules = [];
let _zonesBySource = {};
let _editingRuleId = null;

async function loadStatus() {
  const d = await fetch('/api/settings/status',{cache:'no-store'}).then(r=>r.json()).catch(()=>({}));

  // Disk KPI
  if (d.disk) {
    document.getElementById('diskFree').textContent  = fmt.bytes(d.disk.free);
    const pct = Math.round(d.disk.used / d.disk.total * 100);
    document.getElementById('diskUsedPct').textContent = `of ${fmt.bytes(d.disk.total)} · ${pct}% used`;
  }

  // Footage KPI
  const total = Object.values(d.source_sizes||{}).reduce((a,b)=>a+b,0);
  document.getElementById('videoSize').textContent = fmt.bytes(total);
  document.getElementById('segCount').textContent  = (d.segments||0).toLocaleString();

  // Pipeline KPI + chip
  const threads   = d.recording_threads || {};
  const deadCams  = Object.entries(threads).filter(([,alive])=>!alive).map(([id])=>id);
  const yoloOk    = d.yolo_connected;
  const bfDone    = d.backfill_pending === 0;
  const invalid   = d.backfill_ignored_invalid || 0;
  const ignoredText = invalid > 0 ? ` · ignored ${invalid} invalid clips` : '';
  const anyDead   = deadCams.length > 0;

  const healthEl  = document.getElementById('pipelineHealth');
  const subEl     = document.getElementById('pipelineSub');
  const chipEl    = document.getElementById('pipelineChip');
  const chipTxt   = document.getElementById('pipelineText');

  let healthClass, healthText, subText, chipClass;
  if (anyDead) {
    healthClass = 'dead'; healthText = `${deadCams.length} cam offline`;
    subText = deadCams.join(', ');
    chipClass = 'dead'; chipTxt.textContent = `${deadCams.length} cam offline`;
  } else if (!yoloOk) {
    healthClass = 'warn'; healthText = 'Detection offline';
    subText = 'AI detection paused — check logs';
    chipClass = 'warn'; chipTxt.textContent = 'Detection offline';
  } else if (!bfDone && d.backfill_pending > 0) {
    healthClass = 'warn'; healthText = 'Processing';
    subText = `${d.backfill_pending} clips queued for detection${ignoredText} · last event ${fmt.ts(d.latest_event_ts)}`;
    chipClass = 'warn'; chipTxt.textContent = `Processing: ${d.backfill_pending} clips`;
  } else {
    healthClass = 'ok'; healthText = 'Healthy';
    subText = `Detection active · all clips tagged${ignoredText} · last event ${fmt.ts(d.latest_event_ts)}`;
    chipClass = ''; chipTxt.textContent = 'All systems healthy';
  }

  healthEl.textContent = healthText;
  healthEl.className   = 's-kpi-value ' + healthClass;
  subEl.textContent    = subText;
  chipEl.hidden = false;
  chipEl.className = 's-pipeline-chip ' + chipClass;

  // Per-camera disk bars
  const sizes = document.getElementById('sourceSizes');
  sizes.innerHTML = '';
  for (const [src, bytes] of Object.entries(d.source_sizes||{}).sort((a,b)=>b[1]-a[1])) {
    const row = document.createElement('div');
    row.className = 's-source-row';
    const pct = d.disk?.total ? Math.round(bytes/d.disk.total*100) : 0;
    row.innerHTML = `<span class="s-source-name">${src}</span>
      <div class="s-source-bar"><div class="s-source-fill" style="width:${pct}%"></div></div>
      <span class="s-source-bytes">${fmt.bytes(bytes)}</span>`;
    sizes.appendChild(row);
  }

  _lastThreads = threads;
  // Refresh camera status dots without full reload
  document.querySelectorAll('[data-cam-dot]').forEach(dot => {
    const id    = dot.dataset.camDot;
    const alive = threads[id];
    dot.className = 's-cam-dot' + (alive === true ? ' live' : alive === false ? ' dead' : '');
  });
}

// ── Cameras ───────────────────────────────────────────
async function loadCameras() {
  const d = await fetch('/api/sources',{cache:'no-store'}).then(r=>r.json()).catch(()=>({sources:[]}));
  _sources = d.sources || [];
  const list = document.getElementById('cameraList');
  const cleanupSel = document.getElementById('cleanupSource');
  list.innerHTML = '';
  cleanupSel.innerHTML = '<option value="">All cameras</option>';

  for (const s of _sources) {
    const alive  = _lastThreads[s.id];
    const dotCls = alive === true ? 'live' : alive === false ? 'dead' : '';

    const row = document.createElement('div');
    row.className = 's-cam-row';
    row.innerHTML = `
      <span class="s-cam-dot ${dotCls}" data-cam-dot="${s.id}"></span>
      <div class="s-cam-info">
        <div class="s-cam-name">${s.name||s.id}</div>
        <div class="s-cam-meta">${s.id}</div>
      </div>
      ${s.mutable
        ? `<button class="s-cam-remove" data-del="${s.id}" title="Remove ${s.name||s.id}" type="button" aria-label="Remove camera">
             <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true"><path d="M2.5 2.5l9 9M11.5 2.5l-9 9" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>
           </button>`
        : '<span></span>'}`;
    list.appendChild(row);

    const opt = document.createElement('option');
    opt.value = s.id; opt.textContent = s.name || s.id;
    cleanupSel.appendChild(opt);
  }

  list.querySelectorAll('[data-del]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm(`Remove ${btn.dataset.del}? This cannot be undone.`)) return;
      await fetch('/api/sources/'+btn.dataset.del,{method:'DELETE'});
      loadCameras().then(loadNotificationRules);
    });
  });

  populateNotifySources();
  renderNotificationRules();
}

// ── Add camera drawer ─────────────────────────────────
const showAddBtn   = document.getElementById('showAddBtn');
const cancelAddBtn = document.getElementById('cancelAddBtn');
const drawer       = document.getElementById('addCameraDrawer');

function openDrawer()  { drawer.hidden = false; showAddBtn.hidden = true; }
function closeDrawer() {
  drawer.hidden = true;
  showAddBtn.hidden = false;
  document.getElementById('newName').value = '';
  document.getElementById('newUrl').value  = '';
  document.getElementById('testThumb').hidden = true;
  document.getElementById('testMsg').textContent = '';
  document.getElementById('testMsg').className = 's-test-msg';
  document.getElementById('addBtn').disabled = true;
}

showAddBtn.addEventListener('click', openDrawer);
cancelAddBtn.addEventListener('click', closeDrawer);

// Test
document.getElementById('testBtn').addEventListener('click', async () => {
  const url = document.getElementById('newUrl').value.trim();
  const msg = document.getElementById('testMsg');
  const thumb = document.getElementById('testThumb');
  const addBtn = document.getElementById('addBtn');
  if (!url) { msg.textContent = 'Enter a URL first'; msg.className = 's-test-msg err'; return; }
  msg.textContent = 'Connecting…'; msg.className = 's-test-msg';
  thumb.hidden = true; addBtn.disabled = true;
  try {
    const r = await fetch('/api/settings/camera/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
    if (r.ok) {
      const blob = await r.blob();
      thumb.src = URL.createObjectURL(blob); thumb.hidden = false;
      msg.textContent = 'Connection OK'; msg.className = 's-test-msg ok';
      addBtn.disabled = false;
    } else {
      const e = await r.json().catch(()=>({}));
      msg.textContent = e.error || `Error ${r.status}`; msg.className = 's-test-msg err';
    }
  } catch { msg.textContent = 'Network error'; msg.className = 's-test-msg err'; }
});

// Add
document.getElementById('addBtn').addEventListener('click', async () => {
  const name = document.getElementById('newName').value.trim();
  const url  = document.getElementById('newUrl').value.trim();
  if (!name || !url) return;
  const r = await fetch('/api/sources',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,url})});
  if (r.ok) { closeDrawer(); loadCameras().then(loadNotificationRules); }
});

// ── Notification rules ───────────────────────────────
const showNotifyBtn   = document.getElementById('showNotifyBtn');
const cancelNotifyBtn = document.getElementById('cancelNotifyBtn');
const notifyDrawer    = document.getElementById('notificationDrawer');

function sourceName(sourceId) {
  const source = _sources.find(s => s.id === sourceId);
  return source?.name || sourceId || 'Unknown camera';
}

function classesText(classes) {
  if (!Array.isArray(classes) || classes.length === 0) return 'All objects';
  return classes.join(', ');
}

function cooldownText(seconds) {
  const s = Math.max(0, parseInt(seconds || 0, 10));
  if (!s) return 'No cooldown';
  if (s % 3600 === 0) return `${s / 3600}h cooldown`;
  if (s % 60 === 0) return `${s / 60}m cooldown`;
  return `${s}s cooldown`;
}

function zoneNameForRule(rule) {
  const ref = rule.zone_ref || 'whole_frame';
  if (ref === 'whole_frame') return 'Whole frame';
  if (ref === 'all_activity_areas') return 'All activity areas';
  if (ref.startsWith('zone:')) {
    const uid = ref.slice(5);
    const zone = (_zonesBySource[rule.source_id] || []).find(z => z.uid === uid);
    return zone?.name || 'Missing area';
  }
  return ref;
}

async function loadZonesForSource(sourceId, force = false) {
  if (!sourceId) return [];
  if (!force && _zonesBySource[sourceId]) return _zonesBySource[sourceId];
  const p = new URLSearchParams({ source: sourceId });
  const d = await fetch(`/api/video/zones?${p}`, { cache:'no-store' })
    .then(r => r.json())
    .catch(() => ({ zones: [] }));
  _zonesBySource[sourceId] = (d.zones || []).filter(z => z.enabled !== false);
  return _zonesBySource[sourceId];
}

function populateNotifySources(selectedSource) {
  const sel = document.getElementById('notifySource');
  if (!sel) return;
  const current = selectedSource || sel.value || _sources[0]?.id || '';
  sel.innerHTML = '';
  if (!_sources.length) {
    const opt = document.createElement('option');
    opt.value = ''; opt.textContent = 'No cameras';
    sel.appendChild(opt);
    if (showNotifyBtn) showNotifyBtn.disabled = true;
    return;
  }
  if (showNotifyBtn) showNotifyBtn.disabled = false;
  _sources.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.name || s.id;
    sel.appendChild(opt);
  });
  sel.value = _sources.some(s => s.id === current) ? current : _sources[0].id;
}

async function populateNotifyZones(sourceId, selectedRef = 'whole_frame') {
  const sel = document.getElementById('notifyZone');
  if (!sel) return;
  sel.disabled = true;
  sel.innerHTML = '';
  const base = [
    ['whole_frame', 'Whole frame'],
    ['all_activity_areas', 'All activity areas'],
  ];
  base.forEach(([value, label]) => {
    const opt = document.createElement('option');
    opt.value = value; opt.textContent = label;
    sel.appendChild(opt);
  });
  const zones = await loadZonesForSource(sourceId, true);
  zones.forEach(z => {
    if (!z.uid) return;
    const opt = document.createElement('option');
    opt.value = `zone:${z.uid}`;
    opt.textContent = z.name || `Area ${z.id}`;
    sel.appendChild(opt);
  });
  const values = new Set([...sel.options].map(o => o.value));
  let hasSelected = selectedRef && values.has(selectedRef);
  if (selectedRef && !hasSelected) {
    const opt = document.createElement('option');
    opt.value = selectedRef;
    opt.textContent = 'Missing area';
    opt.disabled = true;
    sel.appendChild(opt);
    hasSelected = true;
  }
  sel.value = hasSelected ? selectedRef : 'whole_frame';
  sel.disabled = false;
}

async function loadNotificationRules() {
  const d = await fetch('/api/notifications/rules', { cache:'no-store' })
    .then(r => r.json())
    .catch(() => ({ rules: [] }));
  _notificationRules = d.rules || [];
  const sources = [...new Set(_notificationRules.map(r => r.source_id).filter(Boolean))];
  await Promise.all(sources.map(sourceId => loadZonesForSource(sourceId)));
  renderNotificationRules();
}

function renderNotificationRules() {
  const list = document.getElementById('notificationRules');
  const count = document.getElementById('notificationCount');
  if (!list) return;
  list.innerHTML = '';
  if (count) count.textContent = `${_notificationRules.length} rule${_notificationRules.length === 1 ? '' : 's'}`;

  if (!_notificationRules.length) {
    const empty = document.createElement('div');
    empty.className = 's-rule-empty';
    empty.textContent = 'No notification rules.';
    list.appendChild(empty);
    return;
  }

  _notificationRules.forEach(rule => {
    const row = document.createElement('div');
    row.className = 's-rule-row' + (rule.enabled ? '' : ' is-paused');

    const main = document.createElement('div');
    main.className = 's-rule-main';

    const title = document.createElement('div');
    title.className = 's-rule-title-line';
    const name = document.createElement('div');
    name.className = 's-rule-name';
    name.textContent = rule.name || 'Notification rule';
    const state = document.createElement('span');
    state.className = 's-rule-pill' + (rule.enabled ? '' : ' paused');
    state.textContent = rule.enabled ? 'Enabled' : 'Paused';
    const delivery = document.createElement('span');
    delivery.className = 's-rule-pill web';
    delivery.textContent = 'Web';
    title.append(name, state, delivery);

    const meta = document.createElement('div');
    meta.className = 's-rule-meta';
    meta.textContent = `${sourceName(rule.source_id)} · ${zoneNameForRule(rule)} · ${classesText(rule.classes)} · ${cooldownText(rule.cooldown_seconds)}`;

    main.append(title, meta);

    const actions = document.createElement('div');
    actions.className = 's-rule-actions';
    const edit = document.createElement('button');
    edit.className = 's-btn';
    edit.type = 'button';
    edit.textContent = 'Edit';
    edit.addEventListener('click', () => openNotifyDrawer(rule));
    const toggle = document.createElement('button');
    toggle.className = 's-btn';
    toggle.type = 'button';
    toggle.textContent = rule.enabled ? 'Pause' : 'Enable';
    toggle.addEventListener('click', () => toggleNotifyRule(rule));
    const del = document.createElement('button');
    del.className = 's-btn s-btn-danger';
    del.type = 'button';
    del.textContent = 'Delete';
    del.addEventListener('click', () => deleteNotifyRule(rule));
    actions.append(edit, toggle, del);

    row.append(main, actions);
    list.appendChild(row);
  });
}

async function openNotifyDrawer(rule = null) {
  if (!_sources.length) return;
  _editingRuleId = rule?.id ?? null;
  document.getElementById('notificationDrawerTitle').textContent = rule ? 'Edit rule' : 'New rule';
  document.getElementById('notifyName').value = rule?.name || '';
  document.getElementById('notifyClasses').value = Array.isArray(rule?.classes) ? rule.classes.join(', ') : '';
  document.getElementById('notifyCooldown').value = rule?.cooldown_seconds ?? 60;
  document.getElementById('notifyEnabled').value = rule?.enabled === false ? '0' : '1';
  document.getElementById('notifyMsg').textContent = '';
  document.getElementById('notifyMsg').className = 's-save-msg';
  populateNotifySources(rule?.source_id);
  const sourceId = document.getElementById('notifySource').value;
  await populateNotifyZones(sourceId, rule?.zone_ref || 'whole_frame');
  notifyDrawer.hidden = false;
  showNotifyBtn.hidden = true;
}

function closeNotifyDrawer() {
  _editingRuleId = null;
  notifyDrawer.hidden = true;
  showNotifyBtn.hidden = false;
  document.getElementById('notifyMsg').textContent = '';
  document.getElementById('notifyMsg').className = 's-save-msg';
}

function collectNotifyRule() {
  const classes = document.getElementById('notifyClasses').value
    .split(',')
    .map(s => s.trim())
    .filter(Boolean);
  return {
    name: document.getElementById('notifyName').value.trim() || 'Notification rule',
    source_id: document.getElementById('notifySource').value,
    zone_ref: document.getElementById('notifyZone').value,
    classes,
    cooldown_seconds: parseInt(document.getElementById('notifyCooldown').value || '60', 10),
    enabled: document.getElementById('notifyEnabled').value === '1',
  };
}

async function saveNotifyRule() {
  const msg = document.getElementById('notifyMsg');
  const body = collectNotifyRule();
  if (!body.source_id) {
    msg.textContent = 'Select a camera';
    msg.className = 's-save-msg err';
    return;
  }
  msg.textContent = 'Saving…';
  msg.className = 's-save-msg';
  const url = _editingRuleId ? `/api/notifications/rules/${_editingRuleId}` : '/api/notifications/rules';
  const method = _editingRuleId ? 'PUT' : 'POST';
  const r = await fetch(url, {
    method,
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) {
    msg.textContent = d.error || `Error ${r.status}`;
    msg.className = 's-save-msg err';
    return;
  }
  closeNotifyDrawer();
  await loadNotificationRules();
}

async function toggleNotifyRule(rule) {
  await fetch(`/api/notifications/rules/${rule.id}`, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ enabled: !rule.enabled }),
  });
  await loadNotificationRules();
}

async function deleteNotifyRule(rule) {
  if (!confirm(`Delete ${rule.name || 'this rule'}?`)) return;
  await fetch(`/api/notifications/rules/${rule.id}`, { method: 'DELETE' });
  await loadNotificationRules();
}

showNotifyBtn.addEventListener('click', () => openNotifyDrawer());
cancelNotifyBtn.addEventListener('click', closeNotifyDrawer);
document.getElementById('saveNotifyBtn').addEventListener('click', saveNotifyRule);
document.getElementById('notifySource').addEventListener('change', e => {
  populateNotifyZones(e.target.value, 'whole_frame');
});

// ── Auto-cleanup ──────────────────────────────────────
async function loadCleanupConfig() {
  const d = await fetch('/api/settings/cleanup-config').then(r=>r.json()).catch(()=>({}));
  const daysEl = document.getElementById('autoDays');
  const gbEl   = document.getElementById('autoGb');
  if (daysEl) daysEl.value = d.cleanup_days ?? '';
  if (gbEl)   gbEl.value   = d.cleanup_max_gb ?? '';
}

document.getElementById('autoSaveBtn').addEventListener('click', async () => {
  const days = document.getElementById('autoDays').value.trim();
  const gb   = document.getElementById('autoGb').value.trim();
  const msg  = document.getElementById('autoMsg');
  msg.textContent = 'Saving…'; msg.className = 's-save-msg';
  const r = await fetch('/api/settings/cleanup-config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      cleanup_days:   days ? parseFloat(days) : null,
      cleanup_max_gb: gb   ? parseFloat(gb)   : null,
    })
  });
  const d = await r.json();
  if (r.ok) {
    msg.textContent = `Saved — ${d.cleanup_days ?? '∞'} days / ${d.cleanup_max_gb ?? '∞'} GB`;
    msg.className = 's-save-msg ok';
  } else {
    msg.textContent = d.error || `Error ${r.status}`;
    msg.className = 's-save-msg err';
  }
});

// ── Manual delete ─────────────────────────────────────
document.getElementById('cleanupBtn').addEventListener('click', async () => {
  const days = parseInt(document.getElementById('cleanupDays').value);
  const src  = document.getElementById('cleanupSource').value || undefined;
  const msg  = document.getElementById('cleanupMsg');
  const cameraLabel = src || 'all cameras';
  if (!confirm(`Delete all footage older than ${days} days from ${cameraLabel}? This cannot be undone.`)) return;
  msg.textContent = 'Deleting…'; msg.className = 's-save-msg';
  const r = await fetch('/api/settings/cleanup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({days,source_id:src})});
  const d = await r.json();
  if (r.ok) {
    msg.textContent = `Deleted ${d.deleted_segments} clips, freed ${fmt.bytes(d.freed_bytes)}`;
    msg.className = 's-save-msg ok';
    loadStatus();
  } else {
    msg.textContent = d.error || `Error ${r.status}`;
    msg.className = 's-save-msg err';
  }
});

// ── Sidebar scroll-spy ────────────────────────────────
const sideLinks = document.querySelectorAll('.s-side-link');
const sections  = ['system','cameras','notifications','storage'].map(id => document.getElementById(id)).filter(Boolean);

const observer = new IntersectionObserver(entries => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      const id = entry.target.id;
      sideLinks.forEach(a => {
        a.classList.toggle('active', a.getAttribute('href') === '#'+id);
      });
    }
  });
}, { threshold: 0.2, rootMargin: '-48px 0px -60% 0px' });

sections.forEach(s => observer.observe(s));

// ── Init ──────────────────────────────────────────────
loadStatus();
loadCameras().then(loadNotificationRules);
loadCleanupConfig();
