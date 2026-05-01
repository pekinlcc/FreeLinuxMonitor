'use strict';

var SEGMENTS = 20;

function fmtBytes(bytes) {
  if (bytes <= 0) return '--- GB';
  var gb = bytes / (1024 * 1024 * 1024);
  if (gb >= 1) return gb.toFixed(1) + ' GB';
  return (bytes / (1024 * 1024)).toFixed(0) + ' MB';
}

function fmtMemBytes(bytes) {
  if (bytes <= 0) return '0';
  var gb = bytes / (1024 * 1024 * 1024);
  if (gb >= 1) return gb.toFixed(1) + 'G';
  return (bytes / (1024 * 1024)).toFixed(0) + 'M';
}

function pctSeverity(pct) {
  return pct >= 90 ? 'crit' : (pct >= 70 ? 'warn' : 'lit');
}

/* ASCII bar for Fallout theme: 20 segments of ▓/░ coloured by severity. */
function renderAsciiBar(el, pct) {
  if (!el) return;
  var segs = SEGMENTS;
  var filled  = Math.max(0, Math.min(segs, Math.round((pct / 100) * segs)));
  var empty   = segs - filled;
  var cls = pctSeverity(pct);
  var bar = '<span class="' + cls + '">';
  for (var i = 0; i < filled; i++) bar += '▓';
  bar += '</span>';
  for (var j = 0; j < empty; j++) bar += '░';
  el.innerHTML = bar;
}

/* Pill-bar fill for Liquid Glass theme. kind = cpu/mem/gpu/disk.
   At crit/warn we swap the gradient to a red/amber tint. */
function renderPillBar(fillEl, pct, kind) {
  if (!fillEl) return;
  fillEl.style.width = Math.max(0, Math.min(100, pct)) + '%';
  var base = ' ' + kind + ' ';
  fillEl.className = 'fill' + base;
  if (pct >= 90)      fillEl.className += 'crit';
  else if (pct >= 70) fillEl.className += 'warn';
}

/* Largest-remainder allocation of N cells across 5 categories. */
function allocateSegments(mem, segs) {
  var total = mem.total > 0 ? mem.total : 1;
  var raw = [
    mem.app / total * segs,
    mem.wired / total * segs,
    mem.compressed / total * segs,
    mem.cached / total * segs,
    mem.free / total * segs
  ];
  var floor = raw.map(Math.floor);
  var used = floor.reduce(function(a, b) { return a + b; }, 0);
  var slack = segs - used;
  var remainders = raw.map(function(r, i) { return { i: i, r: r - floor[i] }; })
                      .sort(function(a, b) { return b.r - a.r; });
  for (var k = 0; k < slack; k++) floor[remainders[k % 5].i] += 1;
  return floor;                   // [app, wired, compressed, cached, free]
}

/* ASCII stacked bar (20 glyphs, 5 colours). */
function renderMemAscii(el, mem) {
  if (!el) return;
  var segs = allocateSegments(mem, SEGMENTS);
  var keys = ['app', 'wire', 'comp', 'cach', 'free'];
  var html = '';
  for (var i = 0; i < 5; i++) {
    if (segs[i] <= 0) continue;
    html += '<span class="' + keys[i] + '">';
    for (var c = 0; c < segs[i]; c++) html += '▓';
    html += '</span>';
  }
  el.innerHTML = html;
}

/* Pill stacked bar — width % per category. */
function renderMemPill(el, mem) {
  if (!el) return;
  var total = mem.total > 0 ? mem.total : 1;
  var pcts = [
    mem.app / total * 100,
    mem.wired / total * 100,
    mem.compressed / total * 100,
    mem.cached / total * 100,
    mem.free / total * 100
  ];
  var kids = el.children;
  for (var i = 0; i < kids.length && i < 5; i++) {
    kids[i].style.width = pcts[i].toFixed(3) + '%';
  }
}

function pad2(n) { return n < 10 ? '0' + n : '' + n; }

function tickClock() {
  var d = new Date();
  var ts = pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  var a = document.getElementById('timestamp');
  var b = document.getElementById('l-time');
  if (a) a.textContent = ts;
  if (b) b.textContent = ts;
}
setInterval(tickClock, 1000);
tickClock();

/* Apply theme / breakdown body classes based on Swift-provided opts. */
function applyMode(opts) {
  var b = document.body;
  b.classList.remove('theme-liquid', 'theme-fallout');
  b.classList.add(opts.theme === 'fallout' ? 'theme-fallout' : 'theme-liquid');
  if (opts.showBreakdown) b.classList.add('show-breakdown');
  else                    b.classList.remove('show-breakdown');
}

window.updateMetrics = function(data, opts) {
  if (typeof data === 'string') {
    try { data = JSON.parse(data); } catch (e) { return; }
  }
  opts = opts || {};
  applyMode(opts);

  // CPU
  var cpu = typeof data.cpu === 'number' ? data.cpu : 0;
  renderAsciiBar(document.getElementById('cpu-bar'), cpu);
  renderPillBar(document.getElementById('cpu-pill'), cpu, 'cpu');
  var cpuPct = document.getElementById('cpu-pct');
  if (cpuPct) cpuPct.textContent = cpu.toFixed(1) + ' %';

  // Memory (single bar used only in no-breakdown mode)
  var mem = typeof data.memory === 'number' ? data.memory : 0;
  renderAsciiBar(document.getElementById('mem-bar'), mem);
  renderPillBar(document.getElementById('mem-pill'), mem, 'mem');
  var memPct = document.getElementById('mem-pct');
  if (memPct) memPct.textContent = mem.toFixed(1) + ' %';

  // Memory breakdown — always populate so the switch is instant
  var mb = data.memBreakdown || null;
  if (mb) {
    renderMemAscii(document.getElementById('mem-stacked-ascii'), mb);
    renderMemPill (document.getElementById('mem-stacked-pill'),  mb);
    var setLabel = function(id, bytes) {
      var el = document.getElementById(id);
      if (el) el.textContent = fmtMemBytes(bytes);
    };
    setLabel('mem-app',  mb.app);
    setLabel('mem-wire', mb.wired);
    setLabel('mem-comp', mb.compressed);
    setLabel('mem-cach', mb.cached);
    setLabel('mem-free', mb.free);
  }

  // GPU
  var gpu = typeof data.gpuUsage === 'number' ? data.gpuUsage : -1;
  var gpuBar  = document.getElementById('gpu-bar');
  var gpuPill = document.getElementById('gpu-pill');
  var gpuPct  = document.getElementById('gpu-pct');
  if (gpu < 0) {
    if (gpuBar)  gpuBar.innerHTML  = '<span style="color:#1a5500;font-size:11px">N/A — NO GPU DATA</span>';
    if (gpuPill) gpuPill.style.width = '0%';
    if (gpuPct)  gpuPct.textContent = 'N/A';
  } else {
    renderAsciiBar(gpuBar, gpu);
    renderPillBar (gpuPill, gpu, 'gpu');
    if (gpuPct) gpuPct.textContent = gpu.toFixed(1) + ' %';
  }

  // Disk
  var diskPct    = typeof data.diskPercent === 'number' ? data.diskPercent : 0;
  var diskUsed   = typeof data.diskUsed    === 'number' ? data.diskUsed    : 0;
  var diskTotal  = typeof data.diskTotal   === 'number' ? data.diskTotal   : 0;
  renderAsciiBar(document.getElementById('disk-bar'), diskPct);
  renderPillBar (document.getElementById('disk-pill'), diskPct, 'disk');
  var diskPctEl  = document.getElementById('disk-pct');
  var diskDetail = document.getElementById('disk-detail');
  if (diskPctEl)  diskPctEl.textContent = diskPct.toFixed(1) + ' %';
  if (diskDetail) diskDetail.textContent = fmtBytes(diskUsed) + ' / ' + fmtBytes(diskTotal);

  // Status (both themes read this; CSS styles diverge via body class)
  var highest = Math.max(cpu, mem, diskPct);
  var statusText, statusClass;
  if      (highest >= 90) { statusText = 'CRITICAL'; statusClass = 'crit'; }
  else if (highest >= 70) { statusText = 'ELEVATED'; statusClass = 'warn'; }
  else                    { statusText = 'NOMINAL';  statusClass = '';     }

  var fs = document.getElementById('status-val');
  if (fs) {
    fs.className = 'status-val' + (statusClass ? ' ' + statusClass : '');
    fs.textContent = statusText;
  }
  var ls = document.getElementById('l-status');
  if (ls) {
    ls.className = 'l-pill' + (statusClass ? ' ' + statusClass : '');
    ls.textContent = statusText;
  }
};

/* Called by Swift after purge finishes. */
window.showReleaseToast = function(amount, hhmmss) {
  var el = document.getElementById('mem-release-toast');
  if (!el) return;
  el.textContent = '▼ Released ' + amount + ' at ' + hhmmss;
  el.classList.add('visible');
  if (window._toastTimer) clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(function() {
    el.classList.remove('visible');
  }, 5000);
};
