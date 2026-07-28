const fmt = {
  bytes: b => b > 1e9 ? (b/1e9).toFixed(1)+'GB' : b > 1e6 ? (b/1e6).toFixed(0)+'MB' : (b/1e3).toFixed(0)+'KB',
  ts:    t => t ? new Date(t*1000).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}) : '--',
  bitrate: b => b == null ? '--' : b >= 1e6 ? `${(b/1e6).toFixed(2)} Mbps` : `${Math.round(b/1e3)} Kbps`,
  age: s => {
    if (s == null || !Number.isFinite(Number(s))) return '--';
    if (s < 1) return '<1s';
    if (s < 60) return `${Math.round(s)}s`;
    if (s < 3600) return `${Math.round(s/60)}m`;
    return `${(s/3600).toFixed(1)}h`;
  },
};

// ── Status (system KPIs + pipeline chip) ─────────────
let _lastThreads = {};
let _sources = [];
let _notificationRules = [];
let _zonesBySource = {};
let _editingRuleId = null;
let _mediaHealth = null;
let _healthHours = 24;
let _healthSource = '';
let _detectionConfig = null;

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

// ── Media health ─────────────────────────────────────
function healthSourceName(sourceId) {
  const source = (_mediaHealth?.sources || _sources).find(s => s.id === sourceId);
  return source?.name || sourceId || 'System';
}

function sourceRecordMode(sourceId) {
  const source = (_mediaHealth?.sources || _sources).find(s => s.id === sourceId);
  return source?.record_mode || 'continuous';
}

function healthState(row) {
  if (!row?.ts) return { cls:'warn', label:'Collecting' };
  const sampleAge = Date.now()/1000 - Number(row.ts || 0);
  // Live-only cameras have no stamper/recorder BY DESIGN, and their relay
  // path is pulled ON DEMAND — it drops to not-ready the moment the last
  // wall viewer leaves. Idle is not offline: without probing the camera we
  // cannot distinguish "nobody watching" from "camera down", so claim
  // neither. Green = streaming right now; neutral = idle.
  if (sourceRecordMode(row.source_id) === 'live_only') {
    if (sampleAge > 60) return { cls:'dead', label:'Offline' };
    if (row.raw_ready) return { cls:'ok', label:'Live-only' };
    return { cls:'', label:'Live-only' };
  }
  const stamper = row.stamper || {};
  const stamperAge = Date.now()/1000 - Number(stamper.updated_at || 0);
  const dead = sampleAge > 60
    || !row.raw_ready || !row.stamped_ready || !row.recorder_thread_alive;
  if (dead) return { cls:'dead', label:'Offline' };
  const warn = row.hls_age_seconds == null
    || Number(row.hls_age_seconds) > 5
    || Number(row.consecutive_failures) > 0
    || stamper.status !== 'live'
    || stamperAge > 45;
  return warn ? { cls:'warn', label:'Degraded' } : { cls:'ok', label:'Healthy' };
}

function renderHealthTable() {
  const body = document.getElementById('healthTable');
  if (!body) return;
  const rows = Object.values(_mediaHealth?.current || {});
  body.innerHTML = '';
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="7" class="s-health-empty">Collecting media health data.</td></tr>';
    return;
  }
  rows.sort((a,b) => healthSourceName(a.source_id).localeCompare(healthSourceName(b.source_id)));
  rows.forEach(row => {
    const state = healthState(row);
    const stamper = row.stamper || {};
    const maxrate = Number(row.maxrate_bps || 0);
    const stamped = Number(row.stamped_bitrate_bps || 0);
    const cap = maxrate && stamped ? Math.round(stamped/maxrate*100) : null;
    const resolution = stamper.width && stamper.height ? `${stamper.width}×${stamper.height}` : '--';
    const fps = stamper.fps ? `${Number(stamper.fps).toFixed(0)} fps` : '';
    const liveOnly = sourceRecordMode(row.source_id) === 'live_only';
    const recorderIssue = liveOnly ? 'live-only'
      : Number(row.consecutive_failures) > 0
      ? `${row.consecutive_failures} failures`
      : row.recorder_thread_alive ? 'running' : 'stopped';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><span class="s-health-camera"></span></td>
      <td><span class="s-health-state ${state.cls}">${state.label}</span></td>
      <td><span class="s-health-metric">${stamper.active_encoder || '--'}</span><span class="s-health-detail">${resolution}${fps ? ' · '+fps : ''}</span></td>
      <td><span class="s-health-metric">${fmt.bitrate(row.raw_bitrate_bps)}</span><span class="s-health-detail">${row.raw_ready ? (liveOnly ? 'streaming (on demand)' : 'relay ready') : (liveOnly ? 'idle — streams on demand' : 'relay offline')}</span></td>
      <td><span class="s-health-metric">${liveOnly ? '--' : fmt.bitrate(row.stamped_bitrate_bps)}</span><span class="s-health-detail">${liveOnly ? 'live-only' : cap == null ? (row.stamped_ready ? 'relay ready' : 'relay offline') : `${cap}% of ${fmt.bitrate(maxrate)}`}</span></td>
      <td><span class="s-health-metric">${liveOnly ? '--' : fmt.age(row.hls_age_seconds)}</span><span class="s-health-detail">${liveOnly ? 'live-only' : row.hls_age_seconds != null && Number(row.hls_age_seconds) <= 5 ? 'fresh' : 'stale'}</span></td>
      <td><span class="s-health-metric">${row.recorder_codec || '--'}</span><span class="s-health-detail">${recorderIssue}</span></td>`;
    ['Camera','State','Encoder','Raw','Stamped','HLS','Recorder'].forEach((label,index) => {
      tr.children[index].dataset.label = label;
    });
    tr.querySelector('.s-health-camera').textContent = healthSourceName(row.source_id);
    body.appendChild(tr);
  });
}

function populateHealthSources() {
  const select = document.getElementById('healthSource');
  if (!select) return;
  const sources = _mediaHealth?.sources || [];
  const previous = _healthSource || select.value;
  select.innerHTML = '';
  sources.forEach(source => {
    const option = document.createElement('option');
    option.value = source.id;
    option.textContent = source.name || source.id;
    select.appendChild(option);
  });
  _healthSource = sources.some(s => s.id === previous) ? previous : (sources[0]?.id || '');
  select.value = _healthSource;
}

function renderHealthEvents() {
  const list = document.getElementById('healthEvents');
  if (!list) return;
  const events = (_mediaHealth?.events || [])
    .filter(event => !_healthSource || event.source_id === _healthSource)
    .slice(0, 20);
  list.innerHTML = '';
  if (!events.length) {
    const empty = document.createElement('div');
    empty.className = 's-health-empty';
    empty.textContent = 'No incidents recorded in this range.';
    list.appendChild(empty);
    return;
  }
  events.forEach(event => {
    const row = document.createElement('div');
    row.className = `s-health-event ${event.severity || 'info'}`;
    const dot = document.createElement('span');
    dot.className = 's-health-event-dot';
    const main = document.createElement('div');
    main.className = 's-health-event-main';
    const source = document.createElement('span');
    source.className = 's-health-event-source';
    source.textContent = healthSourceName(event.source_id);
    const message = document.createElement('span');
    message.textContent = event.message;
    main.append(source, message);
    const when = document.createElement('time');
    when.className = 's-health-event-time';
    when.dateTime = new Date(event.ts*1000).toISOString();
    when.textContent = new Date(event.ts*1000).toLocaleString(undefined, {
      month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'
    });
    row.append(dot, main, when);
    list.appendChild(row);
  });
}

function drawHealthChart() {
  const canvas = document.getElementById('healthChart');
  const empty = document.getElementById('healthChartEmpty');
  if (!canvas || !empty) return;
  const points = (_mediaHealth?.series || []).filter(p => p.source_id === _healthSource);
  const usable = points.filter(p => p.raw_bitrate_bps != null || p.stamped_bitrate_bps != null);
  const rect = canvas.getBoundingClientRect();
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  canvas.width = Math.max(1, Math.round(rect.width*dpr));
  canvas.height = Math.max(1, Math.round(rect.height*dpr));
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr,dpr);
  ctx.clearRect(0,0,rect.width,rect.height);
  if (usable.length < 2) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;

  const styles = getComputedStyle(document.documentElement);
  const colors = {
    grid: styles.getPropertyValue('--line-soft').trim() || '#282a31',
    text: styles.getPropertyValue('--txt-3').trim() || '#7c808b',
    raw: '#5da9e9',
    stamped: styles.getPropertyValue('--green').trim() || '#4ec98a',
    incident: styles.getPropertyValue('--red').trim() || '#e25c4c',
  };
  const pad = {left:52,right:12,top:12,bottom:28};
  const width = rect.width-pad.left-pad.right;
  const height = rect.height-pad.top-pad.bottom;
  const now = Number(_mediaHealth.generated_at || Date.now()/1000);
  const start = now-_healthHours*3600;
  const values = usable.flatMap(p => [p.raw_bitrate_bps,p.stamped_bitrate_bps]).filter(Number.isFinite);
  const maxValue = Math.max(1e6, Math.max(...values)*1.15);
  const x = ts => pad.left + Math.max(0,Math.min(1,(ts-start)/(now-start)))*width;
  const y = value => pad.top + height - Math.max(0,Math.min(1,value/maxValue))*height;

  ctx.font = '10px "IBM Plex Mono", monospace';
  ctx.fillStyle = colors.text;
  ctx.strokeStyle = colors.grid;
  ctx.lineWidth = 1;
  for (let i=0;i<=4;i++) {
    const yy = pad.top + height*i/4;
    ctx.beginPath(); ctx.moveTo(pad.left,yy); ctx.lineTo(rect.width-pad.right,yy); ctx.stroke();
    const value = maxValue*(1-i/4);
    ctx.fillText(value >= 1e6 ? `${(value/1e6).toFixed(1)}M` : `${Math.round(value/1e3)}K`, 4, yy+3);
  }
  for (let i=0;i<=4;i++) {
    const xx = pad.left + width*i/4;
    const ts = start+(now-start)*i/4;
    const label = _healthHours >= 168
      ? new Date(ts*1000).toLocaleDateString(undefined,{weekday:'short'})
      : new Date(ts*1000).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'});
    const measured = ctx.measureText(label).width;
    ctx.fillText(label, Math.max(pad.left,Math.min(rect.width-pad.right-measured,xx-measured/2)), rect.height-8);
  }

  const events = (_mediaHealth.events || []).filter(e => e.source_id === _healthSource);
  ctx.strokeStyle = colors.incident;
  ctx.globalAlpha = 0.45;
  events.forEach(event => {
    const xx = x(event.ts);
    ctx.beginPath(); ctx.moveTo(xx,pad.top); ctx.lineTo(xx,pad.top+height); ctx.stroke();
  });
  ctx.globalAlpha = 1;

  function line(field,color) {
    let drawing = false;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.8;
    ctx.beginPath();
    usable.forEach(point => {
      const value = Number(point[field]);
      if (!Number.isFinite(value)) { drawing = false; return; }
      const px=x(Number(point.ts)), py=y(value);
      if (!drawing) { ctx.moveTo(px,py); drawing=true; } else ctx.lineTo(px,py);
    });
    ctx.stroke();
  }
  line('raw_bitrate_bps',colors.raw);
  line('stamped_bitrate_bps',colors.stamped);
}

// Ordering token: the 30s poll, the refresh button, and the hours switcher
// can overlap — a slow older response must not overwrite a newer one.
let _healthSeq = 0;

async function loadMediaHealth() {
  const seq = ++_healthSeq;
  const updated = document.getElementById('healthUpdated');
  const button = document.getElementById('healthRefreshBtn');
  if (button) button.disabled = true;
  const query = new URLSearchParams({hours:String(_healthHours)});
  const response = await fetch(`/api/settings/media-health?${query}`, {cache:'no-store'}).catch(()=>null);
  if (seq !== _healthSeq) return;   // superseded — the newer call owns the UI
  if (!response?.ok) {
    if (updated) updated.textContent = 'Unavailable';
    if (button) button.disabled = false;
    return;
  }
  const payload = await response.json().catch(() => null);
  if (seq !== _healthSeq || !payload) {
    if (button && seq === _healthSeq) button.disabled = false;
    return;
  }
  _mediaHealth = payload;
  if (updated) updated.textContent = `Updated ${fmt.age(Date.now()/1000-_mediaHealth.generated_at)} ago`;
  populateHealthSources();
  renderHealthTable();
  renderHealthEvents();
  drawHealthChart();
  if (button) button.disabled = false;
}

document.getElementById('healthRefreshBtn')?.addEventListener('click', loadMediaHealth);
document.getElementById('healthSource')?.addEventListener('change', event => {
  _healthSource = event.target.value;
  renderHealthEvents();
  drawHealthChart();
});
document.querySelectorAll('[data-health-hours]').forEach(button => {
  button.addEventListener('click', () => {
    _healthHours = Number(button.dataset.healthHours);
    document.querySelectorAll('[data-health-hours]').forEach(item => {
      item.classList.toggle('active', item === button);
    });
    loadMediaHealth();
  });
});
let _healthResizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(_healthResizeTimer);
  _healthResizeTimer = setTimeout(drawHealthChart, 100);
});

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
        <div class="s-cam-meta">${s.id}${s.record_mode === 'live_only' ? ' · live-only' : ''}</div>
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
      if (!confirm(
        `Remove ${btn.dataset.del}? Camera configuration and media health history will be deleted. Recorded footage is kept.`
      )) return;
      const response = await fetch('/api/sources/'+btn.dataset.del,{method:'DELETE'});
      if (!response.ok) return;
      await loadCameras();
      loadNotificationRules();
      loadMediaHealth();
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
  if (r.ok) { closeDrawer(); loadCameras().then(() => { loadNotificationRules(); }); }
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

// ── Detection classes ────────────────────────────────
function detectionClassLabel(name) {
  return String(name || '').replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function selectedDetectionClasses() {
  return [...document.querySelectorAll('[data-detection-class]:checked')]
    .map(input => input.dataset.detectionClass)
    .filter(Boolean);
}

function updateDetectionSummary() {
  const selected = selectedDetectionClasses();
  const total = (_detectionConfig?.groups || [])
    .reduce((sum, group) => sum + (group.classes || []).length, 0);
  const summary = document.getElementById('detectionSummary');
  const save = document.getElementById('detectionSaveBtn');
  if (summary) {
    summary.textContent = selected.length
      ? `${selected.length} of ${total} model classes enabled`
      : 'Select at least one class';
  }
  if (save) save.disabled = selected.length === 0;
}

function setDetectionSelection(names) {
  const wanted = new Set(names || []);
  document.querySelectorAll('[data-detection-class]').forEach(input => {
    input.checked = wanted.has(input.dataset.detectionClass);
  });
  updateDetectionSummary();
}

function renderDetectionConfig() {
  const root = document.getElementById('detectionGroups');
  if (!root || !_detectionConfig) return;
  root.innerHTML = '';
  (_detectionConfig.groups || []).forEach(group => {
    const section = document.createElement('section');
    section.className = 's-detection-group';
    const title = document.createElement('h3');
    title.className = 's-detection-group-title';
    title.textContent = group.name;
    const options = document.createElement('div');
    options.className = 's-detection-options';
    (group.classes || []).forEach(item => {
      const label = document.createElement('label');
      label.className = 's-detection-option';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.dataset.detectionClass = item.name;
      input.checked = (_detectionConfig.enabled || []).includes(item.name);
      input.addEventListener('change', updateDetectionSummary);
      const text = document.createElement('span');
      text.textContent = detectionClassLabel(item.name);
      label.append(input, text);
      options.appendChild(label);
    });
    section.append(title, options);
    root.appendChild(section);
  });
  updateDetectionSummary();
}

async function loadDetectionConfig() {
  const root = document.getElementById('detectionGroups');
  const d = await fetch('/api/settings/detection-config', {cache:'no-store'})
    .then(async r => r.ok ? r.json() : Promise.reject(new Error(`Error ${r.status}`)))
    .catch(() => null);
  if (!d) {
    if (root) root.innerHTML = '<div class="s-rule-empty">Could not load detection classes.</div>';
    return;
  }
  _detectionConfig = d;
  renderDetectionConfig();
}

document.getElementById('detectionDefaultsBtn')?.addEventListener('click', () => {
  setDetectionSelection(_detectionConfig?.defaults || []);
});
document.getElementById('detectionAllBtn')?.addEventListener('click', () => {
  const all = (_detectionConfig?.groups || [])
    .flatMap(group => (group.classes || []).map(item => item.name));
  setDetectionSelection(all);
});
document.getElementById('detectionNoneBtn')?.addEventListener('click', () => {
  setDetectionSelection([]);
});
document.getElementById('detectionSaveBtn')?.addEventListener('click', async () => {
  const msg = document.getElementById('detectionMsg');
  const classes = selectedDetectionClasses();
  if (!classes.length) return;
  msg.textContent = 'Saving…';
  msg.className = 's-save-msg';
  const r = await fetch('/api/settings/detection-config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({classes}),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) {
    msg.textContent = d.error || `Error ${r.status}`;
    msg.className = 's-save-msg err';
    return;
  }
  _detectionConfig = d;
  renderDetectionConfig();
  msg.textContent = `Saved — ${d.enabled.length} classes active within a few seconds`;
  msg.className = 's-save-msg ok';
});

// ── Auto-cleanup / per-camera retention ───────────────
let _retention = null;
const RETENTION_DAY_CHOICES = [1, 2, 3, 7, 14, 30, 60, 90];

function retentionDaysLabel(days) {
  if (days == null) return 'forever';
  return days === 1 ? '1 day' : `${days} days`;
}

function updateRetentionPreview() {
  const globalRaw = document.getElementById('autoDays')?.value.trim();
  const globalDays = globalRaw ? parseFloat(globalRaw) : null;
  document.querySelectorAll('[data-retention-source]').forEach(select => {
    const sourceId = select.dataset.retentionSource;
    const globalOption = select.querySelector('option[value="global"]');
    if (globalOption) globalOption.textContent = `Use global (${retentionDaysLabel(globalDays)})`;
    const mode = document.querySelector(`[data-record-mode-source="${CSS.escape(sourceId)}"]`)?.value;
    const detail = document.querySelector(`[data-retention-detail="${CSS.escape(sourceId)}"]`);
    if (!detail) return;
    if (mode === 'live_only') {
      detail.textContent = 'Realtime page only — no recording, no detection';
      return;
    }
    const days = select.value === 'global' ? globalDays : parseFloat(select.value);
    detail.textContent = `Keeps footage ${days == null ? 'forever' : retentionDaysLabel(days)}`;
  });
}

function renderRetention() {
  const table = document.getElementById('retentionTable');
  if (!table || !_retention) return;
  const sources = _retention.sources || _sources || [];
  table.innerHTML = '';
  if (!sources.length) {
    table.innerHTML = '<tr><td colspan="3" class="s-quality-empty">No cameras configured.</td></tr>';
    return;
  }
  sources.forEach(source => {
    const sourceId = source.id;
    const row = document.createElement('tr');

    const camera = document.createElement('td');
    camera.dataset.label = 'Camera';
    const name = document.createElement('span');
    name.className = 's-quality-camera';
    name.textContent = source.name || sourceId;
    const id = document.createElement('span');
    id.className = 's-quality-camera-id';
    id.textContent = sourceId;
    camera.append(name, id);

    const modeTd = document.createElement('td');
    modeTd.dataset.label = 'Recording';
    const modeSel = document.createElement('select');
    modeSel.className = 's-quality-select';
    modeSel.dataset.recordModeSource = sourceId;
    [['continuous', 'Continuous'], ['live_only', 'Live only']].forEach(([v, label]) => {
      const opt = document.createElement('option');
      opt.value = v; opt.textContent = label;
      modeSel.appendChild(opt);
    });
    modeSel.value = _retention.record_modes?.[sourceId] || 'continuous';
    modeSel.addEventListener('change', updateRetentionPreview);
    modeTd.appendChild(modeSel);

    const ageTd = document.createElement('td');
    ageTd.dataset.label = 'Max age';
    const ageSel = document.createElement('select');
    ageSel.className = 's-quality-select';
    ageSel.dataset.retentionSource = sourceId;
    const globalOpt = document.createElement('option');
    globalOpt.value = 'global';
    ageSel.appendChild(globalOpt);
    const override = _retention.day_overrides?.[sourceId];
    const choices = [...RETENTION_DAY_CHOICES];
    if (override != null && !choices.includes(override)) choices.push(override);
    choices.sort((a, b) => a - b).forEach(days => {
      const opt = document.createElement('option');
      opt.value = String(days);
      opt.textContent = retentionDaysLabel(days);
      ageSel.appendChild(opt);
    });
    ageSel.value = override != null ? String(override) : 'global';
    ageSel.addEventListener('change', updateRetentionPreview);
    const detail = document.createElement('span');
    detail.className = 's-quality-detail';
    detail.dataset.retentionDetail = sourceId;
    ageTd.append(ageSel, detail);

    row.append(camera, modeTd, ageTd);
    table.appendChild(row);
  });
  updateRetentionPreview();
}

async function loadCleanupConfig() {
  const d = await fetch('/api/settings/cleanup-config').then(r=>r.json()).catch(()=>({}));
  _retention = d;
  const daysEl = document.getElementById('autoDays');
  const gbEl   = document.getElementById('autoGb');
  if (daysEl) { daysEl.value = d.cleanup_days ?? ''; daysEl.oninput = updateRetentionPreview; }
  if (gbEl)   gbEl.value   = d.cleanup_max_gb ?? '';
  renderRetention();
}

document.getElementById('autoSaveBtn').addEventListener('click', async () => {
  const days = document.getElementById('autoDays').value.trim();
  const gb   = document.getElementById('autoGb').value.trim();
  const msg  = document.getElementById('autoMsg');
  const dayOverrides = {};
  document.querySelectorAll('[data-retention-source]').forEach(select => {
    dayOverrides[select.dataset.retentionSource] =
      select.value === 'global' ? null : parseFloat(select.value);
  });
  const recordModes = {};
  document.querySelectorAll('[data-record-mode-source]').forEach(select => {
    recordModes[select.dataset.recordModeSource] = select.value;
  });
  msg.textContent = 'Saving…'; msg.className = 's-save-msg';
  const r = await fetch('/api/settings/cleanup-config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      cleanup_days:   days ? parseFloat(days) : null,
      cleanup_max_gb: gb   ? parseFloat(gb)   : null,
      day_overrides:  dayOverrides,
      record_modes:   recordModes,
    })
  });
  const d = await r.json();
  if (r.ok) {
    _retention = d;
    renderRetention();
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
    msg.textContent = `Deleted ${d.deleted_segments} clips and ${d.deleted_notifications || 0} notifications, freed ${fmt.bytes(d.freed_bytes)}`;
    msg.className = 's-save-msg ok';
    loadStatus();
  } else {
    msg.textContent = d.error || `Error ${r.status}`;
    msg.className = 's-save-msg err';
  }
});

// ── Sidebar scroll-spy ────────────────────────────────
const sideLinks = document.querySelectorAll('.s-side-link');
const sections  = ['system','cameras','detection','media-health','notifications','storage'].map(id => document.getElementById(id)).filter(Boolean);

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
loadCameras().then(() => { loadNotificationRules(); });
loadDetectionConfig();
loadMediaHealth();
loadCleanupConfig();
setInterval(loadMediaHealth, 30000);
