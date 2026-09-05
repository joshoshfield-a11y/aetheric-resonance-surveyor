/* ============================================================
   ARS — Aetheric Resonance Surveyor v5.0
   app.js — all application logic.

   Constraints honoured here:
   - plain script (no modules), no fetch/XHR, no network, no CDN
   - Chrome 100+ WebView compatible syntax (ES2019-ish ceiling)
   - file:// safe: every capability (mic, sensors, subtle crypto,
     localStorage, WebAudio) is feature-detected and guarded
   - Canvas 2D only; localStorage for persistence
   ============================================================ */
(function () {
'use strict';

/* ============================ constants ============================ */

/* OMEGA lattice: 13 beacon node frequencies (Hz) */
var OMEGA_FREQS = [140, 210, 350, 490, 770, 910, 1190, 1330, 1610, 2030, 2170, 2590, 2870];
/* reference amplitude vector for the beacon lattice */
var OMEGA_AMPS  = [0.73, 0.66, 0.57, 0.48, 0.31, 0.22, 0.05, 0.02, 0.12, 0.15, 0.13, 0.007, 0.09];
/* null frequencies watched for >6 dB spikes (Tier-1) */
var NULL_FREQS  = [175, 280, 420, 630, 840];
/* reply composer: per-node amplitude for trit values 0/1/2 */
var REPLY_LEVELS = [0.15, 0.50, 0.85];

var TONE_SECONDS     = 1.0;     /* each beacon tone lasts exactly 1.000 s */
var WAV_RATE         = 44100;   /* WAV render sample rate */
var FFT_SIZE         = 8192;    /* analyser fftSize */
var FOCUS_HZ         = 3000;    /* spectrum display focus band */
var CORR_THRESHOLD   = 0.8;     /* lattice Pearson threshold */
var CORR_SUSTAIN_MS  = 3000;    /* must hold this long for a DETECTION */
var DETECTOR_MS      = 500;     /* lattice detector cadence */
var SPIKE_DB         = 6;       /* null-spike dB over rolling median */
var SPIKE_WINDOW     = 40;      /* rolling median window (samples) */
var SPIKE_COOLDOWN_MS = 10000;  /* per-null re-alert cooldown */
var LS_JOURNAL = 'ars.journal.v1';
var LS_TIERS   = 'ars.tiers.v1';
var LS_REPLY   = 'ars.reply.v1';

/* ============================ DOM helpers ============================ */

function $(id) { return document.getElementById(id); }

function setText(id, txt) {
  var n = $(id);
  if (n) n.textContent = txt;
}

function setStatus(id, msg, cls, led) {
  var n = $(id);
  if (!n) return;
  n.className = 'status-line' + (cls ? ' ' + cls : '');
  n.innerHTML = '<span class="led' + (led ? ' ' + led : '') + '"></span>' + msg;
}

function fmt(v, digits) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  return Number(v).toFixed(digits === undefined ? 2 : digits);
}

function utcStamp(d) {
  return (d || new Date()).toISOString().replace('T', ' ').replace(/\.\d+Z$/, 'Z');
}

function escHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* ============================ safe storage ============================ */
/* localStorage can throw (private mode, locked-down WebView). Fall back
   to an in-memory shim so the app keeps working. */
var storage = (function () {
  var mem = {};
  var ok = false;
  try {
    var probe = '__ars_probe__';
    window.localStorage.setItem(probe, '1');
    window.localStorage.removeItem(probe);
    ok = true;
  } catch (e) { ok = false; }
  return {
    get: function (k) {
      try {
        if (ok) { var v = window.localStorage.getItem(k); return v === null ? null : v; }
        return (k in mem) ? mem[k] : null;
      } catch (e) { return (k in mem) ? mem[k] : null; }
    },
    set: function (k, v) {
      try { if (ok) { window.localStorage.setItem(k, v); return; } } catch (e) {}
      mem[k] = v;
    },
    persistent: ok
  };
})();

/* ============================ SHA-256 ============================ */
/* Journal hashing prefers crypto.subtle (async). Where unavailable
   (some file:// WebViews) a self-contained pure-JS implementation is
   used so the hash chain always works. */
var SHA256_K = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
];

function utf8Bytes(str) {
  var out = [];
  for (var i = 0; i < str.length; i++) {
    var c = str.charCodeAt(i);
    if (c < 0x80) out.push(c);
    else if (c < 0x800) { out.push(0xc0 | (c >> 6), 0x80 | (c & 63)); }
    else if (c >= 0xd800 && c < 0xdc00 && i + 1 < str.length) {
      var c2 = str.charCodeAt(++i);
      var cp = 0x10000 + ((c & 0x3ff) << 10) + (c2 & 0x3ff);
      out.push(0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 63), 0x80 | ((cp >> 6) & 63), 0x80 | (cp & 63));
    } else out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
  }
  return out;
}

function sha256HexSync(str) {
  var bytes = utf8Bytes(str);
  var bitLen = bytes.length * 8;
  bytes.push(0x80);
  while (bytes.length % 64 !== 56) bytes.push(0);
  /* 64-bit length (messages here are far below 2^32 bits) */
  for (var i = 7; i >= 0; i--) bytes.push((bitLen / Math.pow(2, i * 8)) & 0xff);

  var h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
  var w = new Array(64);
  function rr(x, n) { return (x >>> n) | (x << (32 - n)); }

  for (var b = 0; b < bytes.length; b += 64) {
    for (var t = 0; t < 16; t++) {
      w[t] = (bytes[b + t * 4] << 24) | (bytes[b + t * 4 + 1] << 16) |
             (bytes[b + t * 4 + 2] << 8) | bytes[b + t * 4 + 3];
    }
    for (t = 16; t < 64; t++) {
      var s0 = rr(w[t - 15], 7) ^ rr(w[t - 15], 18) ^ (w[t - 15] >>> 3);
      var s1 = rr(w[t - 2], 17) ^ rr(w[t - 2], 19) ^ (w[t - 2] >>> 10);
      w[t] = (w[t - 16] + s0 + w[t - 7] + s1) | 0;
    }
    var a = h[0], bb = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
    for (t = 0; t < 64; t++) {
      var S1 = rr(e, 6) ^ rr(e, 11) ^ rr(e, 25);
      var ch = (e & f) ^ (~e & g);
      var t1 = (hh + S1 + ch + SHA256_K[t] + w[t]) | 0;
      var S0 = rr(a, 2) ^ rr(a, 13) ^ rr(a, 22);
      var mj = (a & bb) ^ (a & c) ^ (bb & c);
      var t2 = (S0 + mj) | 0;
      hh = g; g = f; f = e; e = (d + t1) | 0; d = c; c = bb; bb = a; a = (t1 + t2) | 0;
    }
    h[0] = (h[0] + a) | 0; h[1] = (h[1] + bb) | 0; h[2] = (h[2] + c) | 0; h[3] = (h[3] + d) | 0;
    h[4] = (h[4] + e) | 0; h[5] = (h[5] + f) | 0; h[6] = (h[6] + g) | 0; h[7] = (h[7] + hh) | 0;
  }
  var out = '';
  for (t = 0; t < 8; t++) {
    var hex = (h[t] >>> 0).toString(16);
    out += ('00000000' + hex).slice(-8);
  }
  return out;
}

/* async wrapper: try subtle, fall back to the sync implementation */
function sha256Hex(str) {
  try {
    if (window.crypto && window.crypto.subtle && window.isSecureContext !== false) {
      var data = new Uint8Array(utf8Bytes(str));
      return window.crypto.subtle.digest('SHA-256', data).then(function (buf) {
        var arr = new Uint8Array(buf), out = '';
        for (var i = 0; i < arr.length; i++) out += ('0' + arr[i].toString(16)).slice(-2);
        return out;
      }).catch(function () { return sha256HexSync(str); });
    }
  } catch (e) { /* fall through */ }
  return Promise.resolve(sha256HexSync(str));
}

/* ============================ statistics helpers ============================ */

/* Pearson product-moment correlation of two equal-length vectors */
function pearson(a, b) {
  var n = Math.min(a.length, b.length);
  if (n < 2) return 0;
  var i, ma = 0, mb = 0;
  for (i = 0; i < n; i++) { ma += a[i]; mb += b[i]; }
  ma /= n; mb /= n;
  var num = 0, da = 0, db = 0;
  for (i = 0; i < n; i++) {
    var xa = a[i] - ma, xb = b[i] - mb;
    num += xa * xb; da += xa * xa; db += xb * xb;
  }
  if (da <= 0 || db <= 0) return 0;
  return num / Math.sqrt(da * db);
}

function median(arr) {
  if (!arr.length) return NaN;
  var s = arr.slice().sort(function (a, b) { return a - b; });
  var m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

/* log-gamma (Lanczos approximation) */
function gammaln(x) {
  var c = [0.99999999999980993, 676.5203681218851, -1259.1392167224028,
           771.32342877765313, -176.61502916214059, 12.507343278686905,
           -0.13857109526572012, 9.9843695780195716e-6, 1.5056327351493116e-7];
  if (x < 0.5) return Math.log(Math.PI / Math.sin(Math.PI * x)) - gammaln(1 - x);
  x -= 1;
  var a = c[0];
  for (var i = 1; i < 9; i++) a += c[i] / (x + i);
  var t = x + 7.5;
  return 0.5 * Math.log(2 * Math.PI) + (x + 0.5) * Math.log(t) - t + Math.log(a);
}

/* regularized upper incomplete gamma Q(a, x) — used for chi-square p-values */
function gammaQ(a, x) {
  if (x < 0 || a <= 0) return NaN;
  if (x === 0) return 1;
  var gln = gammaln(a), i, del;
  if (x < a + 1) {
    /* series expansion for P, return 1 - P */
    var ap = a, sum = 1 / a;
    del = sum;
    for (i = 1; i <= 500; i++) {
      ap += 1;
      del *= x / ap;
      sum += del;
      if (Math.abs(del) < Math.abs(sum) * 1e-14) break;
    }
    return Math.max(0, Math.min(1, 1 - sum * Math.exp(-x + a * Math.log(x) - gln)));
  }
  /* continued fraction for Q */
  var b = x + 1 - a, c = 1e300, d = 1 / b, h = d;
  for (i = 1; i <= 500; i++) {
    var an = -i * (i - a);
    b += 2;
    d = an * d + b; if (Math.abs(d) < 1e-300) d = 1e-300;
    c = b + an / c; if (Math.abs(c) < 1e-300) c = 1e-300;
    d = 1 / d;
    del = d * c;
    h *= del;
    if (Math.abs(del - 1) < 1e-14) break;
  }
  return Math.max(0, Math.min(1, Math.exp(-x + a * Math.log(x) - gln) * h));
}

/* chi-square survival function */
function chiSquareP(chi2, df) { return gammaQ(df / 2, chi2 / 2); }

/* ============================ global state ============================ */

var state = {
  bootedAt: Date.now(),
  activeTab: 'monitor',
  journal: [],          /* loaded from storage */
  tierChecks: {},       /* manual tier checkbox state */
  reply: [0,0,0,0,0,0,0,0,0,0,0,0,0],
  flags: {},            /* flagKey -> {ts, label} raised this session */
  mic: { active: false, sampleRate: 0, error: null },
  rng: { running: false, count: 0, p: null, chi2: null, runsZ: null, flagged: false, flagCount: 0 },
  lattice: { r: 0, detected: false },
  audioReady: false
};

/* flag a tab's attention dot (cleared when the tab is visited) */
function markTab(tab) {
  var dot = $('dot-' + tab);
  if (dot) dot.classList.add('on');
}
function clearTab(tab) {
  var dot = $('dot-' + tab);
  if (dot) dot.classList.remove('on');
}

/* ============================ header clock ============================ */

function startHeaderClock() {
  function tick() {
    var now = new Date();
    setText('hdr-utc', utcStamp(now).slice(11)); /* HH:MM:SSZ */
    var up = Math.floor((Date.now() - state.bootedAt) / 1000);
    var h = Math.floor(up / 3600), m = Math.floor((up % 3600) / 60), s = up % 60;
    setText('hdr-uptime',
      ('0' + h).slice(-2) + ':' + ('0' + m).slice(-2) + ':' + ('0' + s).slice(-2));
  }
  tick();
  setInterval(tick, 1000);
}

/* ============================ tab switching ============================ */

function initTabs() {
  var nav = $('tabs');
  var buttons = nav.querySelectorAll('button[data-tab]');
  function activate(name) {
    state.activeTab = name;
    for (var i = 0; i < buttons.length; i++) {
      var b = buttons[i];
      b.classList.toggle('active', b.getAttribute('data-tab') === name);
    }
    var panels = document.querySelectorAll('section.panel');
    for (var j = 0; j < panels.length; j++) {
      panels[j].classList.toggle('active', panels[j].id === 'panel-' + name);
    }
    clearTab(name);
  }
  for (var i = 0; i < buttons.length; i++) {
    (function (b) {
      b.addEventListener('click', function () { activate(b.getAttribute('data-tab')); });
    })(buttons[i]);
  }
  activate('monitor');
  return activate;
}
var activateTab = null; /* assigned during init */

/* ================================================================
   JOURNAL — append-only, hash-chained, localStorage-backed
   hash = SHA-256(prevHash + timestamp + content)
   ================================================================ */

var journal = (function () {
  var GENESIS = 'GENESIS';

  function load() {
    try {
      var raw = storage.get(LS_JOURNAL);
      if (raw) {
        var arr = JSON.parse(raw);
        if (arr && arr.length !== undefined) { state.journal = arr; return; }
      }
    } catch (e) { /* corrupted store -> start fresh, keep app alive */ }
    state.journal = [];
  }

  function persist() {
    try { storage.set(LS_JOURNAL, JSON.stringify(state.journal)); } catch (e) {}
  }

  function renderEntry(entry) {
    var li = document.createElement('li');
    var alertCls = /DETECTION|FLAG|SPIKE|ALERT/i.test(entry.type) ? ' class="alert"' : '';
    var short = entry.hash ? entry.hash.slice(0, 10) : '----------';
    li.innerHTML = '<span class="tag">' + escHtml(entry.type) + '</span>' +
      '<b>' + escHtml(entry.ts) + '</b> ' + escHtml(entry.content) +
      ' <span style="color:var(--text-faint)">#' + short + '</span>';
    if (alertCls) li.className = 'alert';
    return li;
  }

  function render() {
    var list = $('jr-list');
    if (!list) return;
    list.innerHTML = '';
    /* newest first */
    for (var i = state.journal.length - 1; i >= 0; i--) {
      list.appendChild(renderEntry(state.journal[i]));
    }
    setText('jr-count', state.journal.length + (state.journal.length === 1 ? ' entry' : ' entries'));
  }

  /* add an entry; returns a promise resolving to the stored entry */
  function add(type, content) {
    var prev = state.journal.length ? state.journal[state.journal.length - 1].hash : GENESIS;
    var ts = utcStamp();
    return sha256Hex(prev + '|' + ts + '|' + content).then(function (hash) {
      var entry = { ts: ts, type: type, content: String(content), prev: prev, hash: hash };
      state.journal.push(entry);
      persist();
      render();
      markTab('journal');
      return entry;
    });
  }

  /* recompute the whole chain and report integrity */
  function verify() {
    var prev = GENESIS;
    var result = { ok: true, checked: 0, failAt: -1 };
    /* chain promises sequentially since sha256Hex may be async */
    var p = Promise.resolve();
    state.journal.forEach(function (entry, idx) {
      p = p.then(function () {
        return sha256Hex(prev + '|' + entry.ts + '|' + entry.content).then(function (h) {
          if (result.ok && (h !== entry.hash || entry.prev !== prev)) {
            result.ok = false; result.failAt = idx;
          }
          prev = entry.hash;
          result.checked++;
        });
      });
    });
    return p.then(function () { return result; });
  }

  function clear() {
    state.journal = [];
    persist();
    render();
  }

  return { load: load, add: add, verify: verify, render: render, clear: clear, GENESIS: GENESIS };
})();

/* raise an instrument flag: journal it, auto-tick tiers, light tab dots */
function raiseFlag(flagKey, tierKey, type, content) {
  var first = !state.flags[flagKey];
  state.flags[flagKey] = { ts: utcStamp(), label: type };
  if (first) {
    journal.add(type, content);
  }
  if (tierKey) tierAutoTick(tierKey);
  return first;
}

/* placeholder wired once TIERS module initialises (avoids load-order coupling) */
function tierAutoTick(key) {
  if (window.ARS && window.ARS.api && window.ARS.api.tierAutoTick) {
    window.ARS.api.tierAutoTick(key);
  }
}

/* ================================================================
   AUDIO ENGINE — shared WebAudio plumbing for BEACON + MONITOR
   Everything lazy and guarded: AudioContext is only created after a
   user gesture; if unavailable the UI degrades, never throws.
   ================================================================ */

var audio = (function () {
  var ctx = null;
  var analyser = null;
  var micStream = null;
  var micSource = null;

  function supported() {
    return !!(window.AudioContext || window.webkitAudioContext);
  }

  /* create/resume the shared AudioContext (call from user gestures) */
  function ensureContext() {
    if (!supported()) return null;
    if (!ctx) {
      try {
        var AC = window.AudioContext || window.webkitAudioContext;
        ctx = new AC();
        analyser = ctx.createAnalyser();
        analyser.fftSize = FFT_SIZE;
        analyser.smoothingTimeConstant = 0.5;
        analyser.minDecibels = -100;
        analyser.maxDecibels = -30;
      } catch (e) {
        ctx = null;
        return null;
      }
    }
    if (ctx && ctx.state === 'suspended') {
      /* resume is best-effort; ignore rejection (autoplay policies) */
      try { ctx.resume().catch(function () {}); } catch (e) {}
    }
    return ctx;
  }

  function getAnalyser() { return analyser; }
  function getContext() { return ctx; }

  /* sin² amplitude envelope samples across one second of tone */
  function sin2Curve(samples) {
    var c = new Float32Array(samples);
    for (var i = 0; i < samples; i++) {
      var ph = Math.PI * (i / (samples - 1));
      var s = Math.sin(ph);
      c[i] = s * s;
    }
    return c;
  }
  var cachedCurve = null;
  function getCurve(sr) {
    if (!cachedCurve || cachedCurve.length !== Math.floor(sr * TONE_SECONDS)) {
      cachedCurve = sin2Curve(Math.floor(sr * TONE_SECONDS));
    }
    return cachedCurve;
  }

  /* schedule the 13-tone lattice into a given BaseAudioContext.
     nodeAmps: 13 amplitudes. startAt: ctx time. Returns {stop, endTime, sources}. */
  function scheduleLattice(targetCtx, nodeAmps, startAt) {
    var sr = targetCtx.sampleRate;
    var curve = getCurve(sr);
    var master = targetCtx.createGain();
    master.gain.value = 1.0;
    master.connect(targetCtx.destination);
    var sources = [];
    for (var k = 0; k < 13; k++) {
      var osc = targetCtx.createOscillator();
      osc.type = 'sine';
      osc.frequency.value = OMEGA_FREQS[k];
      var g = targetCtx.createGain();
      var t0 = startAt + k * TONE_SECONDS;
      g.gain.setValueAtTime(0, t0);
      g.gain.setValueCurveAtTime(
        multiplyCurve(curve, nodeAmps[k]), t0, TONE_SECONDS);
      osc.connect(g);
      g.connect(master);
      osc.start(t0);
      osc.stop(t0 + TONE_SECONDS + 0.02);
      sources.push(osc);
    }
    return {
      endTime: startAt + 13 * TONE_SECONDS,
      stop: function () {
        for (var i = 0; i < sources.length; i++) {
          try { sources[i].stop(); } catch (e) {}
        }
        try { master.disconnect(); } catch (e) {}
      }
    };
  }

  function multiplyCurve(curve, scale) {
    var out = new Float32Array(curve.length);
    for (var i = 0; i < curve.length; i++) out[i] = curve[i] * scale;
    return out;
  }

  /* stop mic capture and release the track */
  function stopMic() {
    if (micStream) {
      try {
        var tracks = micStream.getTracks();
        for (var i = 0; i < tracks.length; i++) tracks[i].stop();
      } catch (e) {}
    }
    if (micSource) { try { micSource.disconnect(); } catch (e) {} }
    micStream = null;
    micSource = null;
    state.mic.active = false;
  }

  /* start mic capture -> analyser. Returns Promise<boolean>. */
  function startMic() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      state.mic.error = 'mediaDevices missing — origin "' + location.origin + '" secure=' +
        window.isSecureContext + ' (needs secure context; APK v5.0.1+ serves https origin)';
      return Promise.resolve(false);
    }
    var c = ensureContext();
    if (!c) { state.mic.error = 'AudioContext unavailable'; return Promise.resolve(false); }
    if (micStream) { state.mic.active = true; return Promise.resolve(true); }
    return navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
    }).then(function (stream) {
      micStream = stream;
      micSource = c.createMediaStreamSource(stream);
      micSource.connect(analyser);   /* analysis only, no playback path */
      state.mic.active = true;
      state.mic.sampleRate = c.sampleRate;
      state.mic.error = null;
      return true;
    }).catch(function (err) {
      var name = (err && err.name) ? err.name : 'getUserMedia failed';
      var hints = {
        NotAllowedError: 'permission denied — Android Settings > Apps > ARS > Permissions > Microphone, then retry',
        NotFoundError: 'no microphone device found',
        NotReadableError: 'mic is busy in another app — close it and retry',
        SecurityError: 'insecure context blocked mic — update to APK v5.0.1+',
        OverconstrainedError: 'mic rejected the requested constraints'
      };
      state.mic.error = name + ((err && err.message) ? ' (' + err.message + ')' : '') +
        (hints[name] ? ' — ' + hints[name] : '');
      state.mic.active = false;
      return false;
    });
  }

  /* render the lattice offline and return a Float32Array (mono) */
  function renderOffline(nodeAmps) {
    if (!window.OfflineAudioContext && !window.webkitOfflineAudioContext) {
      return Promise.reject(new Error('OfflineAudioContext unavailable'));
    }
    var OAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    var len = Math.ceil(13 * TONE_SECONDS * WAV_RATE);
    var off = new OAC(1, len, WAV_RATE);
    scheduleLattice(off, nodeAmps, 0);
    return off.startRendering().then(function (buf) {
      return buf.getChannelData(0);
    });
  }

  return {
    supported: supported,
    ensureContext: ensureContext,
    getContext: getContext,
    getAnalyser: getAnalyser,
    startMic: startMic,
    stopMic: stopMic,
    scheduleLattice: scheduleLattice,
    renderOffline: renderOffline
  };
})();

/* ================================================================
   WAV ENCODING — 16-bit PCM mono, RIFF/WAVE
   ================================================================ */

function encodeWavPCM16(samples, sampleRate) {
  var n = samples.length;
  var buf = new ArrayBuffer(44 + n * 2);
  var v = new DataView(buf);
  function str(off, s) { for (var i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); }
  str(0, 'RIFF');
  v.setUint32(4, 36 + n * 2, true);
  str(8, 'WAVE');
  str(12, 'fmt ');
  v.setUint32(16, 16, true);        /* fmt chunk size */
  v.setUint16(20, 1, true);         /* PCM */
  v.setUint16(22, 1, true);         /* mono */
  v.setUint32(24, sampleRate, true);
  v.setUint32(28, sampleRate * 2, true); /* byte rate */
  v.setUint16(32, 2, true);         /* block align */
  v.setUint16(34, 16, true);        /* bits */
  str(36, 'data');
  v.setUint32(40, n * 2, true);
  for (var i = 0; i < n; i++) {
    var s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
  return new Uint8Array(buf);
}

/* chunked base64 (btoa on the whole MB string can overflow the stack).
   CHUNK must be a multiple of 3 so '=' padding only ever appears in the
   final chunk — otherwise the concatenated string is not valid base64. */
function bytesToBase64(bytes) {
  var out = '';
  var CHUNK = 49152; /* 48 KiB, divisible by 3 */
  for (var i = 0; i < bytes.length; i += CHUNK) {
    var sub = bytes.subarray(i, Math.min(i + CHUNK, bytes.length));
    out += btoa(String.fromCharCode.apply(null, sub));
  }
  return out;
}

/* ================================================================
   BEACON PANEL — TX, WAV export, REPLY composer, RX decode, spec
   ================================================================ */

var beacon = (function () {
  var txTimer = null;        /* node-indicator interval */
  var txActive = null;       /* handle from scheduleLattice */
  var replyActive = null;
  var rx = { active: false, timer: null, phase: 'idle', windowIdx: 0, amps: [] };

  /* ---------- shared helpers ---------- */

  function setLed(el, on) { if (el) el.classList.toggle('lit', !!on); }

  function lightNode(containerId, idx) {
    var strip = $(containerId);
    if (!strip) return;
    var cells = strip.children;
    for (var i = 0; i < cells.length; i++) setLed(cells[i], i === idx);
  }

  function clearNodes(containerId) { lightNode(containerId, -1); }

  function buildNodeStrip() {
    var strip = $('tx-nodes');
    if (!strip) return;
    strip.innerHTML = '';
    for (var i = 0; i < 13; i++) {
      var cell = document.createElement('div');
      cell.className = 'node-cell';
      cell.innerHTML = '<span class="f">' + OMEGA_FREQS[i] + '</span>n' + (i + 1);
      strip.appendChild(cell);
    }
  }

  /* ---------- TX beacon ---------- */

  function stopTx(silent) {
    if (txTimer) { clearInterval(txTimer); txTimer = null; }
    if (txActive) { txActive.stop(); txActive = null; }
    clearNodes('tx-nodes');
    var btn = $('btn-tx'), stop = $('btn-tx-stop');
    if (btn) btn.disabled = false;
    if (stop) stop.disabled = true;
    if (!silent) setStatus('tx-status', 'ABORTED — partial transmission not a valid lattice', 'warn', 'warn');
  }

  function playTx() {
    var ctx = audio.ensureContext();
    if (!ctx) {
      setStatus('tx-status', 'AUDIO UNAVAILABLE — WebAudio not supported in this WebView', 'bad', 'bad');
      return;
    }
    stopTx(true);
    var startAt = ctx.currentTime + 0.08;
    txActive = audio.scheduleLattice(ctx, OMEGA_AMPS, startAt);
    $('btn-tx').disabled = true;
    $('btn-tx-stop').disabled = false;
    setStatus('tx-status', 'TRANSMITTING — OMEGA lattice, 13.000 s total', 'warn', 'warn');
    journal.add('TX', 'OMEGA beacon transmitted (13 nodes, 1.000 s tones, sin² envelope)');

    var t0 = Date.now() + 80;
    txTimer = setInterval(function () {
      var elapsed = (Date.now() - t0) / 1000;
      var idx = Math.floor(elapsed / TONE_SECONDS);
      if (idx >= 13) {
        stopTx(true);
        setStatus('tx-status', 'TX COMPLETE — lattice cycle finished', 'ok', 'on');
        journal.add('TX', 'OMEGA beacon cycle complete');
        return;
      }
      lightNode('tx-nodes', idx);
      setStatus('tx-status', 'TRANSMITTING — node ' + (idx + 1) + '/13 @ ' + OMEGA_FREQS[idx] + ' Hz', 'warn', 'warn');
    }, 60);
  }

  /* ---------- WAV export ---------- */

  function exportWav(nodeAmps, label) {
    var status = label === 'reply' ? 'reply-status' : 'tx-status';
    setStatus(status, 'RENDERING — OfflineAudioContext @ ' + WAV_RATE + ' Hz…', 'warn', 'warn');
    audio.renderOffline(nodeAmps).then(function (samples) {
      var bytes = encodeWavPCM16(samples, WAV_RATE);
      var b64 = bytesToBase64(bytes);
      var area = $('wav-b64'), link = $('wav-link'), out = $('wav-out');
      if (out) out.style.display = 'block';
      if (area) area.value = b64;
      if (link) {
        try {
          var blob = new Blob([bytes], { type: 'audio/wav' });
          if (link.dataset.url) { try { URL.revokeObjectURL(link.dataset.url); } catch (e) {} }
          var url = URL.createObjectURL(blob);
          link.dataset.url = url;
          link.href = url;
          link.setAttribute('download', label === 'reply' ? 'omega_reply.wav' : 'omega_beacon.wav');
        } catch (e) { link.style.display = 'none'; }
      }
      setText('wav-meta', label + ' · ' + WAV_RATE + ' Hz · 16-bit mono · ' +
        (bytes.length / 1024).toFixed(1) + ' KiB · 13.000 s');
      setStatus(status, 'WAV READY — download link and base64 fallback below', 'ok', 'on');
      journal.add('EXPORT', 'WAV rendered (' + label + ', ' + bytes.length + ' bytes)');
    }).catch(function (err) {
      setStatus(status, 'WAV RENDER FAILED — ' + (err && err.message ? err.message : 'offline audio unavailable'), 'bad', 'bad');
    });
  }

  /* ---------- reply composer ---------- */

  function replyString() {
    return state.reply.join('');
  }

  function renderTrits() {
    var strip = $('reply-trits');
    if (!strip) return;
    strip.innerHTML = '';
    for (var i = 0; i < 13; i++) {
      (function (idx) {
        var cell = document.createElement('div');
        cell.className = 'trit-cell' + (state.reply[idx] ? ' t' + state.reply[idx] : '');
        cell.textContent = String(state.reply[idx]);
        cell.title = 'node ' + (idx + 1) + ' · ' + OMEGA_FREQS[idx] + ' Hz';
        cell.addEventListener('click', function () {
          state.reply[idx] = (state.reply[idx] + 1) % 3;
          storage.set(LS_REPLY, replyString());
          renderTrits();
        });
        strip.appendChild(cell);
      })(i);
    }
    setText('reply-string', replyString());
  }

  function replyAmps() {
    var out = [];
    for (var i = 0; i < 13; i++) out.push(REPLY_LEVELS[state.reply[i]]);
    return out;
  }

  function stopReply(silent) {
    if (replyActive) { replyActive.stop(); replyActive = null; }
    if (!silent) setStatus('reply-status', 'REPLY ABORTED', 'warn', 'warn');
  }

  function playReply() {
    var ctx = audio.ensureContext();
    if (!ctx) {
      setStatus('reply-status', 'AUDIO UNAVAILABLE — WebAudio not supported', 'bad', 'bad');
      return;
    }
    stopReply(true);
    var startAt = ctx.currentTime + 0.08;
    replyActive = audio.scheduleLattice(ctx, replyAmps(), startAt);
    setStatus('reply-status', 'PLAYING REPLY — ' + replyString(), 'warn', 'warn');
    journal.add('TX', 'reply lattice played: ' + replyString());
    setTimeout(function () {
      replyActive = null;
      setStatus('reply-status', 'REPLY COMPLETE — ' + replyString(), 'ok', 'on');
    }, 13 * TONE_SECONDS * 1000 + 200);
  }

  /* ---------- RX decode (best-effort acoustic) ---------- */

  function rxStop(msg, cls, led) {
    rx.active = false;
    if (rx.timer) { clearInterval(rx.timer); rx.timer = null; }
    var stop = $('btn-rx-stop'), btn = $('btn-rx');
    if (stop) stop.disabled = true;
    if (btn) btn.disabled = false;
    if (msg) setStatus('rx-status', msg, cls, led);
  }

  function rxDisplay(result, amps) {
    var grouped = result.slice(0, 5) + ' ' + result.slice(5, 10) + ' ' + result.slice(10);
    setText('rx-result', grouped);
    setText('rx-amps', amps.map(function (a) { return a.toFixed(3); }).join(' '));
  }

  function rxListen() {
    setStatus('rx-status', 'ARMING — starting mic path…', 'warn', 'warn');
    audio.startMic().then(function (okMic) {
      if (!okMic) {
        rxStop('MIC UNAVAILABLE — ' + (state.mic.error || 'permission denied') +
          '. RX decode needs microphone access.', 'bad', 'bad');
        markTab('beacon');
        return;
      }
      var analyser = audio.getAnalyser();
      var ctx = audio.getContext();
      if (!analyser || !ctx) { rxStop('AUDIO UNAVAILABLE', 'bad', 'bad'); return; }
      rx.active = true;
      rx.phase = 'onset';
      rx.amps = [];
      rx.windowIdx = 0;
      $('btn-rx').disabled = true;
      $('btn-rx-stop').disabled = false;
      setText('rx-result', '····· ······ ···');
      setText('rx-amps', 'listening…');
      setStatus('rx-status', 'LISTENING — waiting for lattice onset (140 Hz gate)…', 'warn', 'warn');

      var bins = new Float32Array(analyser.frequencyBinCount);
      var binHz = ctx.sampleRate / analyser.fftSize;
      var onsetHits = 0;
      var winSamples = [];
      var winStart = 0;
      var onsetTimeout = 0;

      function nodePower(freq) {
        analyser.getFloatFrequencyData(bins);
        var b = freq / binHz;
        var i0 = Math.floor(b), i1 = Math.min(i0 + 1, bins.length - 1);
        var frac = b - i0;
        var db = bins[i0] * (1 - frac) + bins[i1] * frac;
        return Math.pow(10, db / 10);
      }

      rx.timer = setInterval(function () {
        if (!rx.active) return;
        if (rx.phase === 'onset') {
          /* gate: strong energy at node 1 (140 Hz) relative to broadband */
          var p140 = nodePower(OMEGA_FREQS[0]);
          var wide = 0;
          for (var f = 100; f <= 3000; f += 100) wide += nodePower(f);
          var avg = wide / 30;
          if (p140 > avg * 8) {
            onsetHits++;
          } else {
            onsetHits = 0;
          }
          onsetTimeout += 100;
          if (onsetTimeout >= 60000) {
            rxStop('TIMEOUT — no lattice onset heard in 60 s', 'warn', 'warn');
            return;
          }
          if (onsetHits >= 3) {
            rx.phase = 'decode';
            rx.windowIdx = 0;
            winSamples = [];
            winStart = Date.now();
            journal.add('RX', 'lattice onset detected; decoding 13 windows');
            setStatus('rx-status', 'ONSET — decoding window 1/13 (' + OMEGA_FREQS[0] + ' Hz)…', 'warn', 'warn');
          }
          return;
        }
        /* decode phase: accumulate power samples within the current 1 s window */
        var now = Date.now();
        var k = rx.windowIdx;
        winSamples.push(nodePower(OMEGA_FREQS[k]));
        if (now - winStart >= 1000) {
          var mean = 0;
          for (var i = 0; i < winSamples.length; i++) mean += winSamples[i];
          mean /= Math.max(1, winSamples.length);
          rx.amps.push(Math.sqrt(mean)); /* amplitude ~ sqrt(power) */
          rx.windowIdx++;
          winSamples = [];
          winStart = now;
          if (rx.windowIdx >= 13) {
            /* normalise and classify each window to nearest reply level */
            var max = 0;
            for (i = 0; i < 13; i++) max = Math.max(max, rx.amps[i]);
            if (max <= 0) max = 1;
            var result = '';
            for (i = 0; i < 13; i++) {
              var norm = (rx.amps[i] / max) * REPLY_LEVELS[2];
              var best = 0, bd = Infinity;
              for (var t = 0; t < 3; t++) {
                var d = Math.abs(norm - REPLY_LEVELS[t]);
                if (d < bd) { bd = d; best = t; }
              }
              result += String(best);
            }
            rxDisplay(result, rx.amps);
            rxStop('DECODE COMPLETE — ' + result + ' · verify by repetition', 'ok', 'on');
            journal.add('RX', 'decoded reply lattice: ' + result +
              ' (amps ' + rx.amps.map(function (a) { return a.toFixed(3); }).join(',') + ')');
            markTab('beacon');
            return;
          }
          setStatus('rx-status', 'DECODING — window ' + (rx.windowIdx + 1) + '/13 (' +
            OMEGA_FREQS[rx.windowIdx] + ' Hz)…', 'warn', 'warn');
        }
      }, 100);
    });
  }

  /* ---------- static signal spec ---------- */

  function renderSpec() {
    var n = $('beacon-spec');
    if (!n) return;
    var rows = [
      ['name', 'OMEGA beacon lattice'],
      ['nodes', '13 sequential tones'],
      ['frequencies', OMEGA_FREQS.join(', ') + ' Hz'],
      ['tone duration', '1.000 s each (13.000 s total)'],
      ['envelope', 'sin² gate across each second'],
      ['ref amplitudes', OMEGA_AMPS.join(', ')],
      ['nulls (watch)', NULL_FREQS.join(', ') + ' Hz'],
      ['reply levels', 'trit 0→0.15 · 1→0.50 · 2→0.85'],
      ['render rate', WAV_RATE + ' Hz 16-bit mono WAV']
    ];
    var html = '';
    for (var i = 0; i < rows.length; i++) {
      html += '<span class="k">' + escHtml(rows[i][0]) + '</span><span class="v">' +
        escHtml(rows[i][1]) + '</span>';
    }
    n.innerHTML = html;
  }

  /* ---------- wiring ---------- */

  function init() {
    buildNodeStrip();
    renderSpec();

    try {
      var saved = storage.get(LS_REPLY);
      if (saved && /^[012]{13}$/.test(saved)) {
        for (var i = 0; i < 13; i++) state.reply[i] = parseInt(saved.charAt(i), 10);
      }
    } catch (e) {}
    renderTrits();

    on('btn-tx', playTx);
    on('btn-tx-stop', function () { stopTx(false); });
    on('btn-tx-wav', function () { exportWav(OMEGA_AMPS, 'beacon'); });
    on('btn-wav-copy', function () {
      var area = $('wav-b64');
      if (area && area.value) copyText(area.value, 'btn-wav-copy');
    });
    on('btn-reply-play', playReply);
    on('btn-reply-wav', function () { exportWav(replyAmps(), 'reply'); });
    on('btn-reply-random', function () {
      var buf = new Uint8Array(13);
      if (window.crypto && window.crypto.getRandomValues) {
        window.crypto.getRandomValues(buf);
        for (var i = 0; i < 13; i++) state.reply[i] = buf[i] % 3;
      } else {
        for (var j = 0; j < 13; j++) state.reply[j] = Math.floor(Math.random() * 3);
      }
      storage.set(LS_REPLY, replyString());
      renderTrits();
    });
    on('btn-reply-clear', function () {
      for (var i = 0; i < 13; i++) state.reply[i] = 0;
      storage.set(LS_REPLY, replyString());
      renderTrits();
    });
    on('btn-rx', rxListen);
    on('btn-rx-stop', function () { rxStop('CANCELLED', 'warn', 'warn'); });
  }

  return { init: init };
})();

/* small event helper used by all panels */
function on(id, fn) {
  var n = $(id);
  if (n) n.addEventListener('click', fn);
}

/* clipboard: navigator.clipboard when present, else select+execCommand */
function copyText(text, btnId) {
  function done(ok) {
    var b = $(btnId);
    if (!b) return;
    var old = b.textContent;
    b.textContent = ok ? 'COPIED ✓' : 'COPY FAILED';
    setTimeout(function () { b.textContent = old; }, 1200);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
    return;
  }
  try {
    var ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    done(true);
  } catch (e) { done(false); }
}

/* ================================================================
   MONITOR PANEL — mic capture, scrolling spectrum, waveform,
   lattice detector, null-spike watch, 13 Hz watch, 40 Hz watch
   ================================================================ */

var monitor = (function () {
  var detectorTimer = null;
  var freqBins = null;       /* Float32Array for getFloatFrequencyData */
  var timeBins = null;       /* Float32Array for getFloatTimeDomainData */
  var binHz = 0;

  /* lattice detector state */
  var corrHistory = [];      /* recent {t, r} samples */
  var detected = false;
  var nodeWindow = [];       /* ring of per-node power vectors (13 s max-hold) */
  var NODE_WINDOW_TICKS = 26; /* 26 x 500 ms = 13 s = one full lattice cycle */

  /* null-spike watch state */
  var nullHist = {};         /* freq -> array of recent dB values */
  var nullCooldown = {};     /* freq -> last alert epoch ms */
  NULL_FREQS.forEach(function (f) { nullHist[f] = []; nullCooldown[f] = 0; });

  /* 13 Hz coherent-tone watch state */
  var tone13 = { window: [], presentSince: 0, flagged: false, drift: null };
  var TONE13_WINDOW_MS = 12000;

  /* 40 Hz >3σ watch state (Tier-1 near-TX criterion) */
  var sig40 = { hist: [], cooldown: 0 };

  /* ---------- mic UI ---------- */

  function refreshMicUi() {
    var start = $('btn-mic-start'), stop = $('btn-mic-stop');
    if (state.mic.active) {
      if (start) start.disabled = true;
      if (stop) stop.disabled = false;
      setStatus('mon-mic-status', 'CAPTURING — analyser live', 'ok', 'on');
      setText('mon-samplerate', 'SR ' + state.mic.sampleRate + ' Hz');
      setText('mon-binhz', fmt(state.mic.sampleRate / FFT_SIZE, 3) + ' Hz/bin');
    } else {
      if (start) start.disabled = false;
      if (stop) stop.disabled = true;
      if (state.mic.error) {
        setStatus('mon-mic-status', 'MIC UNAVAILABLE — ' + escHtml(state.mic.error) +
          ' — detectors idle, other panels unaffected', 'bad', 'bad');
      }
    }
  }

  function startCapture() {
    setStatus('mon-mic-status', 'REQUESTING MIC — grant permission if prompted…', 'warn', 'warn');
    audio.startMic().then(function (ok) {
      if (!ok) { refreshMicUi(); return; }
      var analyser = audio.getAnalyser();
      freqBins = new Float32Array(analyser.frequencyBinCount);
      timeBins = new Float32Array(analyser.fftSize);
      binHz = audio.getContext().sampleRate / analyser.fftSize;
      refreshMicUi();
      startDetector();
    });
  }

  function stopCapture() {
    audio.stopMic();
    state.mic.error = null;
    stopDetector();
    setStatus('mon-mic-status', 'MIC IDLE — capture stopped', '', '');
    setText('mon-samplerate', 'SR —');
    setText('mon-binhz', '—');
    var start = $('btn-mic-start'), stop = $('btn-mic-stop');
    if (start) start.disabled = false;
    if (stop) stop.disabled = true;
  }

  /* ---------- analyser helpers ---------- */

  /* interpolated magnitude (dB) at an arbitrary frequency */
  function dbAt(freq) {
    var b = freq / binHz;
    var i0 = Math.max(0, Math.floor(b));
    var i1 = Math.min(i0 + 1, freqBins.length - 1);
    var frac = b - i0;
    return freqBins[i0] * (1 - frac) + freqBins[i1] * frac;
  }

  function powerAt(freq) { return Math.pow(10, dbAt(freq) / 10); }

  /* parabolic peak interpolation near a target frequency */
  function peakNear(freq, halfWidthBins) {
    var center = Math.round(freq / binHz);
    var best = center, bestDb = -Infinity;
    var lo = Math.max(1, center - halfWidthBins), hi = Math.min(freqBins.length - 2, center + halfWidthBins);
    for (var i = lo; i <= hi; i++) {
      if (freqBins[i] > bestDb) { bestDb = freqBins[i]; best = i; }
    }
    var y0 = freqBins[best - 1], y1 = freqBins[best], y2 = freqBins[best + 1];
    var denom = (y0 - 2 * y1 + y2);
    var shift = denom !== 0 ? 0.5 * (y0 - y2) / denom : 0;
    shift = Math.max(-1, Math.min(1, shift));
    return { freq: (best + shift) * binHz, db: y1 };
  }

  /* ---------- lattice detector (500 ms cadence) ---------- */

  function startDetector() {
    if (detectorTimer) return;
    detectorTimer = setInterval(detectorTick, DETECTOR_MS);
  }

  function stopDetector() {
    if (detectorTimer) { clearInterval(detectorTimer); detectorTimer = null; }
  }

  function detectorTick() {
    var analyser = audio.getAnalyser();
    if (!analyser || !state.mic.active) return;
    analyser.getFloatFrequencyData(freqBins);
    var now = Date.now();

    /* --- 13-node lattice correlation --- */
    /* The beacon transmits its 13 tones SEQUENTIALLY (1 s each), so an
       instantaneous spectrum has only one active node and can never match
       the full reference shape. Detection therefore correlates a 13 s
       sliding max-hold vector (one complete lattice cycle) against the
       reference amplitudes. For a simultaneous lattice the max-hold vector
       equals the instantaneous one, so both cases are covered. */
    var vec = [];
    for (var i = 0; i < 13; i++) vec.push(powerAt(OMEGA_FREQS[i]));
    nodeWindow.push(vec);
    if (nodeWindow.length > NODE_WINDOW_TICKS) nodeWindow.shift();
    var maxVec = [];
    for (i = 0; i < 13; i++) {
      var mx = 0;
      for (var w = 0; w < nodeWindow.length; w++) mx = Math.max(mx, nodeWindow[w][i]);
      maxVec.push(mx);
    }
    /* normalise (relative shape matters, absolute level does not) */
    var mean = 0;
    for (i = 0; i < 13; i++) mean += maxVec[i];
    mean /= 13;
    if (mean <= 0) mean = 1e-12;
    var norm = maxVec.map(function (v) { return v / mean; });
    var r = pearson(norm, OMEGA_AMPS);
    state.lattice.r = r;

    corrHistory.push({ t: now, r: r });
    while (corrHistory.length && now - corrHistory[0].t > CORR_SUSTAIN_MS + DETECTOR_MS) {
      corrHistory.shift();
    }

    var sustained = false;
    if (r > CORR_THRESHOLD) {
      sustained = true;
      for (i = 0; i < corrHistory.length; i++) {
        if (corrHistory[i].r <= CORR_THRESHOLD) { sustained = false; break; }
      }
      if (corrHistory.length * DETECTOR_MS < CORR_SUSTAIN_MS) sustained = false;
    }

    setText('mon-corr-val', fmt(r, 3));
    var fill = $('mon-corr-fill');
    if (fill) {
      var pct = Math.max(0, Math.min(1, (r + 1) / 2)) * 100; /* map [-1,1] -> [0,100] */
      fill.style.width = pct + '%';
      fill.classList.toggle('hot', r > CORR_THRESHOLD);
    }

    if (sustained && !detected) {
      detected = true;
      state.lattice.detected = true;
      setStatus('mon-lattice-status', 'DETECTION — lattice correlation ' + fmt(r, 3) +
        ' sustained ' + (CORR_SUSTAIN_MS / 1000) + ' s', 'ok', 'on');
      journal.add('DETECTION', 'lattice correlation r=' + fmt(r, 3) + ' sustained ' +
        (CORR_SUSTAIN_MS / 1000) + ' s; node vector [' +
        norm.map(function (v) { return fmt(v, 3); }).join(',') + ']');
      markTab('monitor');
    } else if (!sustained && detected) {
      detected = false;
      state.lattice.detected = false;
      setStatus('mon-lattice-status', 'CARRIER LOST — correlation dropped below threshold', 'warn', 'warn');
    } else if (!detected) {
      setStatus('mon-lattice-status',
        r > 0.5 ? 'PARTIAL — r=' + fmt(r, 3) + ' (need >0.80 sustained)'
                : 'NO CARRIER — r=' + fmt(r, 3), r > 0.5 ? 'warn' : '', r > 0.5 ? 'warn' : '');
    }

    drawNodeBars(norm);

    /* --- null-spike watch --- */
    NULL_FREQS.forEach(function (f) {
      var db = dbAt(f);
      var hist = nullHist[f];
      hist.push(db);
      if (hist.length > SPIKE_WINDOW) hist.shift();
      if (hist.length >= 20) {
        var med = median(hist.slice(0, hist.length - 1));
        if (db > med + SPIKE_DB && now - nullCooldown[f] > SPIKE_COOLDOWN_MS) {
          nullCooldown[f] = now;
          var msg = '+' + fmt(db - med, 1) + ' dB spike @ ' + f + ' Hz (median ' + fmt(med, 1) + ' dB)';
          addSpike(msg);
          setStatus('mon-spike-status', 'SPIKE — ' + msg, 'bad', 'bad');
          raiseFlag('spike:' + f, 't1-spike', 'SPIKE', 'null-frequency spike: ' + msg);
          markTab('monitor');
        }
      }
    });

    /* --- 13 Hz coherent-tone watch --- */
    var pk = peakNear(13, 2);
    var bandMed = median([dbAt(6), dbAt(20), dbAt(27), dbAt(33)]);
    var present = pk.db > bandMed + 10;
    tone13.window.push({ t: now, f: pk.freq, present: present });
    while (tone13.window.length && now - tone13.window[0].t > TONE13_WINDOW_MS) tone13.window.shift();

    var fMin = Infinity, fMax = -Infinity, presentAll = true, span = 0;
    if (tone13.window.length > 1) {
      span = now - tone13.window[0].t;
      for (i = 0; i < tone13.window.length; i++) {
        var w = tone13.window[i];
        if (!w.present) presentAll = false;
        fMin = Math.min(fMin, w.f); fMax = Math.max(fMax, w.f);
      }
    }
    tone13.drift = (fMax - fMin);
    setText('mon-13hz-drift', tone13.window.length > 1 ? fmt(tone13.drift, 3) + ' Hz' : '—');
    setText('mon-13hz-time', presentAll ? fmt(span / 1000, 1) + ' s' : '0 s');

    if (presentAll && span > 10000 && tone13.drift < 0.1) {
      if (!tone13.flagged) {
        tone13.flagged = true;
        setStatus('mon-13hz-status', 'COHERENT — <0.1 Hz drift for ' + fmt(span / 1000, 1) +
          ' s (est., resolution-limited)', 'ok', 'on');
        raiseFlag('tone13', 't2-13hz', 'DETECTION',
          'coherent 13 Hz tone, est. drift ' + fmt(tone13.drift, 3) + ' Hz over ' + fmt(span / 1000, 1) + ' s');
        markTab('monitor');
      }
    } else {
      tone13.flagged = false;
      setStatus('mon-13hz-status', presentAll
        ? 'TRACKING — tone present, drift ' + fmt(tone13.drift, 3) + ' Hz (window ' + fmt(span / 1000, 1) + ' s)'
        : 'NO TONE — 13 Hz region not coherent above band floor', presentAll ? 'warn' : '', presentAll ? 'warn' : '');
    }

    /* --- 40 Hz >3σ watch (Tier-1 near-TX) --- */
    var p40 = dbAt(40);
    sig40.hist.push(p40);
    if (sig40.hist.length > 60) sig40.hist.shift();
    if (sig40.hist.length >= 40) {
      var h = sig40.hist.slice(0, sig40.hist.length - 1);
      var m40 = 0;
      for (i = 0; i < h.length; i++) m40 += h[i];
      m40 /= h.length;
      var var40 = 0;
      for (i = 0; i < h.length; i++) var40 += (h[i] - m40) * (h[i] - m40);
      var sd40 = Math.sqrt(var40 / h.length);
      if (sd40 > 0 && p40 > m40 + 3 * sd40 && now - sig40.cooldown > SPIKE_COOLDOWN_MS) {
        sig40.cooldown = now;
        raiseFlag('sigma40', 't1-40hz', 'FLAG', '40 Hz power ' + fmt((p40 - m40) / sd40, 1) +
          'σ above rolling baseline (' + fmt(p40, 1) + ' dB vs ' + fmt(m40, 1) + ' dB)');
        markTab('monitor');
      }
    }
  }

  /* ---------- spike list ---------- */

  function addSpike(msg) {
    var list = $('mon-spike-list');
    if (!list) return;
    var li = document.createElement('li');
    li.className = 'alert';
    li.innerHTML = '<span class="tag">SPIKE</span><b>' + utcStamp() + '</b> ' + escHtml(msg);
    list.insertBefore(li, list.firstChild);
    while (list.children.length > 50) list.removeChild(list.lastChild);
  }

  /* ---------- canvases ---------- */

  /* color ramp for the spectrogram: charcoal -> teal -> amber */
  function ramp(t) {
    t = Math.max(0, Math.min(1, t));
    var r, g, b;
    if (t < 0.5) {
      var u = t * 2;
      r = 16 + (74 - 16) * u; g = 19 + (125 - 19) * u; b = 21 + (120 - 21) * u;
    } else {
      u = (t - 0.5) * 2;
      r = 74 + (216 - 74) * u; g = 125 + (162 - 125) * u; b = 120 + (74 - 120) * u;
    }
    return 'rgb(' + (r | 0) + ',' + (g | 0) + ',' + (b | 0) + ')';
  }

  var specLast = 0;
  function drawSpectrum() {
    var cv = $('cv-spectrum');
    if (!cv) return;
    var now = performance.now();
    if (now - specLast < 90) return; /* ~11 rows/s scroll rate */
    specLast = now;
    var ctx2 = cv.getContext('2d');
    var W = cv.width, H = cv.height;

    /* scroll existing waterfall down 2 px */
    ctx2.drawImage(cv, 0, 2);

    var rowH = 2;
    var img = ctx2.createImageData(W, rowH);
    if (state.mic.active && freqBins && audio.getAnalyser()) {
      audio.getAnalyser().getFloatFrequencyData(freqBins);
      for (var x = 0; x < W; x++) {
        var f = (x / W) * FOCUS_HZ;
        var db = dbAt(f);
        var t = (db - (-100)) / ((-30) - (-100)); /* map dB window to 0..1 */
        /* inline ramp (integer) */
        t = Math.max(0, Math.min(1, t));
        var r, g, b;
        if (t < 0.5) { var u = t * 2; r = 16 + 58 * u; g = 19 + 106 * u; b = 21 + 99 * u; }
        else { u = (t - 0.5) * 2; r = 74 + 142 * u; g = 125 + 37 * u; b = 120 - 46 * u; }
        for (var y = 0; y < rowH; y++) {
          var o = (y * W + x) * 4;
          img.data[o] = r; img.data[o + 1] = g; img.data[o + 2] = b; img.data[o + 3] = 255;
        }
      }
    } else {
      for (var p = 0; p < img.data.length; p += 4) {
        img.data[p] = 16; img.data[p + 1] = 19; img.data[p + 2] = 21; img.data[p + 3] = 255;
      }
    }
    ctx2.putImageData(img, 0, 0);

    /* frequency grid + labels (drawn fresh each pass, static positions) */
    ctx2.fillStyle = 'rgba(20,23,26,0.55)';
    ctx2.fillRect(0, H - 16, W, 16);
    ctx2.strokeStyle = 'rgba(216,162,74,0.12)';
    ctx2.fillStyle = 'rgba(154,149,140,0.8)';
    ctx2.font = '10px monospace';
    ctx2.textAlign = 'center';
    for (var f2 = 0; f2 <= FOCUS_HZ; f2 += 500) {
      var gx = (f2 / FOCUS_HZ) * W;
      ctx2.beginPath(); ctx2.moveTo(gx, 0); ctx2.lineTo(gx, H); ctx2.stroke();
      /* clamp edge labels so they are not clipped */
      ctx2.textAlign = f2 === 0 ? 'left' : (f2 === FOCUS_HZ ? 'right' : 'center');
      ctx2.fillText(f2 === 0 ? '0' : (f2 / 1000) + 'k', gx, H - 4);
    }
    ctx2.textAlign = 'center';
    /* mark beacon nodes */
    ctx2.fillStyle = 'rgba(216,162,74,0.5)';
    for (var i = 0; i < 13; i++) {
      var nx = (OMEGA_FREQS[i] / FOCUS_HZ) * W;
      ctx2.fillRect(nx, H - 20, 1, 4);
    }
  }

  function drawWave() {
    var cv = $('cv-wave');
    if (!cv) return;
    var ctx2 = cv.getContext('2d');
    var W = cv.width, H = cv.height;
    ctx2.fillStyle = '#101315';
    ctx2.fillRect(0, 0, W, H);
    ctx2.strokeStyle = 'rgba(74,125,120,0.25)';
    ctx2.beginPath(); ctx2.moveTo(0, H / 2); ctx2.lineTo(W, H / 2); ctx2.stroke();

    ctx2.strokeStyle = '#d8a24a';
    ctx2.lineWidth = 1.5;
    ctx2.beginPath();
    if (state.mic.active && timeBins && audio.getAnalyser()) {
      audio.getAnalyser().getFloatTimeDomainData(timeBins);
      var step = Math.max(1, Math.floor(timeBins.length / W));
      for (var x = 0; x < W; x++) {
        var v = timeBins[Math.min(timeBins.length - 1, x * step)];
        var y = H / 2 - v * (H / 2) * 0.9;
        if (x === 0) ctx2.moveTo(x, y); else ctx2.lineTo(x, y);
      }
    } else {
      ctx2.moveTo(0, H / 2); ctx2.lineTo(W, H / 2);
    }
    ctx2.stroke();
    ctx2.lineWidth = 1;
  }

  /* per-node bar graph: measured (amber) vs reference shape (teal ticks) */
  function drawNodeBars(norm) {
    var cv = $('cv-nodes');
    if (!cv) return;
    var ctx2 = cv.getContext('2d');
    var W = cv.width, H = cv.height;
    ctx2.fillStyle = '#101315';
    ctx2.fillRect(0, 0, W, H);
    var bw = W / 13;
    /* measured vector is normalised to mean 1; map it back onto the
       reference amplitude scale so bars and outlines are comparable */
    var refMean = 0;
    for (var i = 0; i < 13; i++) refMean += OMEGA_AMPS[i];
    refMean /= 13;
    var refMax = OMEGA_AMPS[0]; /* 0.73 */

    for (i = 0; i < 13; i++) {
      var x = i * bw;
      /* reference outline (teal) */
      var rh = (OMEGA_AMPS[i] / refMax) * 0.85 * (H - 16);
      ctx2.strokeStyle = 'rgba(74,125,120,0.8)';
      ctx2.strokeRect(x + 4, H - 14 - rh, bw - 8, rh);
      /* measured bar (amber) */
      if (norm) {
        var scaled = norm[i] * refMean;
        var mh = Math.min(H - 16, (scaled / refMax) * 0.85 * (H - 16));
        ctx2.fillStyle = 'rgba(216,162,74,0.75)';
        ctx2.fillRect(x + 4, H - 14 - mh, bw - 8, mh);
      }
      ctx2.fillStyle = 'rgba(154,149,140,0.85)';
      ctx2.font = '9px monospace';
      ctx2.textAlign = 'center';
      ctx2.fillText(String(OMEGA_FREQS[i]), x + bw / 2, H - 3);
    }
  }

  /* ---------- animation loop ---------- */

  function animate() {
    try {
      if (state.activeTab === 'monitor') {
        drawSpectrum();
        drawWave();
      }
      sensors.draw();
      rng.draw();
    } catch (e) { /* never let rendering kill the loop */ }
    window.requestAnimationFrame(animate);
  }

  /* ---------- wiring ---------- */

  function init() {
    on('btn-mic-start', startCapture);
    on('btn-mic-stop', stopCapture);
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      state.mic.error = 'mediaDevices missing at init — origin "' + location.origin +
        '" secure=' + window.isSecureContext;
      refreshMicUi();
    }
    if (!audio.supported()) {
      setStatus('mon-mic-status', 'MIC UNAVAILABLE — WebAudio not supported in this WebView', 'bad', 'bad');
      var b = $('btn-mic-start'); if (b) b.disabled = true;
    }
    drawNodeBars(null);
    window.requestAnimationFrame(animate);
  }

  return { init: init };
})();

/* ================================================================
   SENSORS PANEL — Generic Sensor API with devicemotion fallback.
   Sparklines, magnetic jerk flag, 70/140 Hz vibration estimate.
   ================================================================ */

var sensors = (function () {
  var SPARK_MAX = 240;               /* samples kept per sparkline */
  var data = {
    mag:  { x: null, y: null, z: null, mag: null, hist: [], lastMag: null, lastT: 0,
            jerk: null, jerkStreak: 0, flagged: false, src: '—', live: false },
    acc:  { x: null, y: null, z: null, hist: [], src: '—', live: false,
            crossings: [], lastSign: 0, lastVal: 0, hp: 0, vib: null, vibSince: 0, flagged: false },
    gyro: { x: null, y: null, z: null, hist: [], src: '—', live: false },
    lux:  { v: null, hist: [], live: false }
  };

  function pushHist(hist, v) {
    hist.push(v);
    if (hist.length > SPARK_MAX) hist.shift();
  }

  /* ---------- Generic Sensor API attempt ---------- */

  function tryGeneric(name, onReading, onFail) {
    if (!(name in window)) { onFail('not in window'); return false; }
    try {
      var s = new window[name]({ frequency: 30 });
      var gotReading = false;
      s.addEventListener('reading', function () {
        gotReading = true;
        onReading(s);
      });
      s.addEventListener('error', function (ev) {
        if (!gotReading) onFail((ev && ev.error && ev.error.name) || 'sensor error');
      });
      s.start();
      return true;
    } catch (e) {
      onFail(e && e.name ? e.name : 'construct failed');
      return false;
    }
  }

  /* ---------- magnetometer ---------- */

  function magUpdate(x, y, z) {
    var m = data.mag;
    m.x = x; m.y = y; m.z = z;
    m.mag = Math.sqrt(x * x + y * y + z * z);
    pushHist(m.hist, m.mag);
    var now = Date.now();
    if (m.lastMag !== null && m.lastT) {
      var dt = (now - m.lastT) / 1000;
      if (dt > 0) {
        m.jerk = Math.abs(m.mag - m.lastMag) / dt;
        /* Tier-3: sudden magnetic jerk > 20 µT/s sustained (3 samples) */
        if (m.jerk > 20) {
          m.jerkStreak++;
          if (m.jerkStreak >= 3 && !m.flagged) {
            m.flagged = true;
            setStatus('sen-mag-status', 'JERK — |dB/dt| ' + fmt(m.jerk, 1) + ' µT/s sustained', 'bad', 'bad');
            raiseFlag('magjerk', null, 'FLAG', 'magnetic jerk ' + fmt(m.jerk, 1) +
              ' µT/s sustained over 3 samples');
            markTab('sensors');
          }
        } else {
          m.jerkStreak = 0;
          if (m.flagged && m.jerk < 5) {
            m.flagged = false;
            setStatus('sen-mag-status', 'NOMINAL — field steady', 'ok', 'on');
          }
        }
      }
    }
    m.lastMag = m.mag;
    m.lastT = now;
    if (!m.flagged) setStatus('sen-mag-status', 'LIVE — ' + m.src, 'ok', 'on');
    m.live = true;
    setText('sen-mag-x', fmt(x, 2)); setText('sen-mag-y', fmt(y, 2));
    setText('sen-mag-z', fmt(z, 2)); setText('sen-mag-mag', fmt(m.mag, 2));
    setText('sen-mag-jerk', m.jerk === null ? '—' : fmt(m.jerk, 1));
  }

  function magFail(reason) {
    data.mag.src = 'none';
    setText('sen-mag-src', 'src —');
    setStatus('sen-mag-status', 'UNAVAILABLE — ' + reason, 'bad', 'bad');
  }

  /* ---------- accelerometer + vibration estimate ---------- */

  function accUpdate(x, y, z) {
    var a = data.acc;
    a.x = x; a.y = y; a.z = z;
    var mag = Math.sqrt(x * x + y * y + z * z);
    pushHist(a.hist, mag);
    var now = Date.now();

    /* crude high-pass: subtract slow baseline to expose vibration */
    a.hp = a.hp * 0.98 + mag * 0.02;
    var v = mag - a.hp;
    var sign = v > 0 ? 1 : (v < 0 ? -1 : 0);
    if (sign !== 0 && a.lastSign !== 0 && sign !== a.lastSign) {
      a.crossings.push(now);
      while (a.crossings.length && now - a.crossings[0] > 2000) a.crossings.shift();
      /* zero-crossing frequency ≈ crossings / 2 / windowSeconds */
      a.vib = (a.crossings.length / 2) / 2.0;
    }
    if (sign !== 0) a.lastSign = sign;

    if (a.vib !== null) {
      setText('sen-vib', fmt(a.vib, 1));
      var near = (Math.abs(a.vib - 70) <= 3 || Math.abs(a.vib - 140) <= 3);
      if (near) {
        if (!a.vibSince) a.vibSince = now;
        if (now - a.vibSince > 3000 && !a.flagged) {
          a.flagged = true;
          setStatus('sen-vib-status', 'VIBRATION — ' + fmt(a.vib, 1) +
            ' Hz near 70/140 Hz target, sustained >3 s', 'bad', 'bad');
          raiseFlag('vib', 't3-vib', 'FLAG', 'vibration estimate ' + fmt(a.vib, 1) +
            ' Hz (zero-crossing) near 70/140 Hz, sustained');
          markTab('sensors');
        } else if (!a.flagged) {
          setStatus('sen-vib-status', 'NEAR TARGET — ' + fmt(a.vib, 1) + ' Hz, watching…', 'warn', 'warn');
        }
      } else {
        a.vibSince = 0;
        if (a.flagged) a.flagged = false;
        setStatus('sen-vib-status', 'WATCHING — est. ' + fmt(a.vib, 1) + ' Hz', 'ok', 'on');
      }
    }
    a.live = true;
    setText('sen-acc-x', fmt(x, 2)); setText('sen-acc-y', fmt(y, 2)); setText('sen-acc-z', fmt(z, 2));
  }

  function accFail(reason) {
    setStatus('sen-acc-status', 'UNAVAILABLE — ' + reason, 'bad', 'bad');
    setStatus('sen-vib-status', 'VIBRATION WATCH OFFLINE — no accelerometer', 'bad', 'bad');
  }

  /* ---------- gyroscope ---------- */

  function gyroUpdate(x, y, z) {
    var g = data.gyro;
    g.x = x; g.y = y; g.z = z;
    pushHist(g.hist, Math.sqrt(x * x + y * y + z * z));
    g.live = true;
    setText('sen-gyro-x', fmt(x, 3)); setText('sen-gyro-y', fmt(y, 3)); setText('sen-gyro-z', fmt(z, 3));
  }

  /* ---------- ambient light ---------- */

  function luxUpdate(v) {
    data.lux.v = v;
    pushHist(data.lux.hist, v);
    data.lux.live = true;
    setText('sen-lux', fmt(v, 1));
    setStatus('sen-lux-status', 'LIVE — AmbientLightSensor', 'ok', 'on');
  }

  /* ---------- init: try generic API, fall back to events ---------- */

  function init() {
    /* magnetometer: Generic Sensor API only (no useful DOM-event fallback) */
    if (!tryGeneric('Magnetometer', function (s) {
          data.mag.src = 'Generic Sensor API';
          setText('sen-mag-src', 'src sensor-api');
          magUpdate(s.x || 0, s.y || 0, s.z || 0);
        }, magFail)) {
      /* tryGeneric already reported */
    }

    /* accelerometer */
    var accOk = tryGeneric('Accelerometer', function (s) {
      data.acc.src = 'Generic Sensor API';
      setText('sen-acc-src', 'src sensor-api');
      setStatus('sen-acc-status', 'LIVE — sensor-api', 'ok', 'on');
      accUpdate(s.x || 0, s.y || 0, s.z || 0);
    }, function () {});
    if (!accOk) {
      /* fallback: devicemotion */
      var gotMotion = false;
      window.addEventListener('devicemotion', function (ev) {
        var a = ev.accelerationIncludingGravity || ev.acceleration;
        if (!a || (a.x === null && a.y === null)) return;
        if (!gotMotion) {
          gotMotion = true;
          data.acc.src = 'devicemotion';
          setText('sen-acc-src', 'src devicemotion');
          setStatus('sen-acc-status', 'LIVE — devicemotion fallback (includes gravity)', 'ok', 'on');
        }
        accUpdate(a.x || 0, a.y || 0, a.z || 0);
        var r = ev.rotationRate;
        if (r && (r.alpha !== null || r.beta !== null || r.gamma !== null)) {
          if (!data.gyro.live) {
            data.gyro.src = 'devicemotion';
            setText('sen-gyro-src', 'src devicemotion');
            setStatus('sen-gyro-status', 'LIVE — devicemotion rotationRate', 'ok', 'on');
          }
          /* deg/s -> rad/s */
          var D2R = Math.PI / 180;
          gyroUpdate((r.alpha || 0) * D2R, (r.beta || 0) * D2R, (r.gamma || 0) * D2R);
        }
      });
      setTimeout(function () {
        if (!gotMotion && !data.acc.live) accFail('no devicemotion events and no Accelerometer API');
        if (!data.gyro.live) {
          setText('sen-gyro-src', 'src —');
          setStatus('sen-gyro-status', 'UNAVAILABLE — no Gyroscope API or rotationRate events', 'bad', 'bad');
        }
      }, 4000);
    }

    /* gyroscope via Generic Sensor API (only if not already fed) */
    tryGeneric('Gyroscope', function (s) {
      data.gyro.src = 'Generic Sensor API';
      setText('sen-gyro-src', 'src sensor-api');
      setStatus('sen-gyro-status', 'LIVE — sensor-api', 'ok', 'on');
      gyroUpdate(s.x || 0, s.y || 0, s.z || 0);
    }, function (reason) {
      if (!data.gyro.live) {
        setText('sen-gyro-src', 'src —');
        setStatus('sen-gyro-status', 'UNAVAILABLE — ' + reason, 'bad', 'bad');
      }
    });

    /* ambient light */
    tryGeneric('AmbientLightSensor', function (s) {
      luxUpdate(s.illuminance);
    }, function (reason) {
      setText('sen-lux', '—');
      setStatus('sen-lux-status', 'UNAVAILABLE — ' + reason, 'bad', 'bad');
    });
  }

  /* ---------- sparkline drawing (called from the shared rAF loop) ---------- */

  function drawSpark(id, hist, color, minV, maxV) {
    var cv = $(id);
    if (!cv) return;
    var ctx2 = cv.getContext('2d');
    var W = cv.width, H = cv.height;
    ctx2.fillStyle = '#101315';
    ctx2.fillRect(0, 0, W, H);
    if (!hist.length) return;
    var lo = minV, hi = maxV;
    if (lo === undefined || hi === undefined) {
      lo = Infinity; hi = -Infinity;
      for (var i = 0; i < hist.length; i++) { lo = Math.min(lo, hist[i]); hi = Math.max(hi, hist[i]); }
      if (hi - lo < 1e-6) { hi = lo + 1; }
      var pad = (hi - lo) * 0.15;
      lo -= pad; hi += pad;
    }
    ctx2.strokeStyle = color;
    ctx2.beginPath();
    for (i = 0; i < hist.length; i++) {
      var x = (i / (SPARK_MAX - 1)) * W;
      var y = H - 4 - ((hist[i] - lo) / (hi - lo)) * (H - 8);
      if (i === 0) ctx2.moveTo(x, y); else ctx2.lineTo(x, y);
    }
    ctx2.stroke();
  }

  function draw() {
    if (state.activeTab !== 'sensors') return;
    drawSpark('cv-mag', data.mag.hist, '#d8a24a');
    drawSpark('cv-acc', data.acc.hist, '#4a7d78');
    drawSpark('cv-gyro', data.gyro.hist, '#4a7d78');
    drawSpark('cv-lux', data.lux.hist, '#d8a24a');
  }

  return { init: init, draw: draw, data: data };
})();

/* ================================================================
   RNG PANEL — crypto.getRandomValues stream, chi-square p-value,
   runs test, p-history strip chart. Tier-2 flag at p < 0.001.
   ================================================================ */

var rng = (function () {
  var SAMPLE_BYTES = 256;
  var SAMPLE_MS = 500;
  var HIST_MAX = 240;
  var timer = null;
  var pHist = [];

  function chiSquareOf(bytes) {
    var counts = new Array(SAMPLE_BYTES);
    for (var i = 0; i < SAMPLE_BYTES; i++) counts[i] = 0;
    for (i = 0; i < bytes.length; i++) counts[bytes[i]]++;
    var expected = bytes.length / SAMPLE_BYTES;
    var chi2 = 0;
    for (i = 0; i < SAMPLE_BYTES; i++) {
      var d = counts[i] - expected;
      chi2 += (d * d) / expected;
    }
    return chi2;
  }

  /* runs test over the bit stream; returns z-score */
  function runsTestZ(bytes) {
    var n = bytes.length * 8;
    var ones = 0, runs = 1, prev = -1;
    for (var i = 0; i < bytes.length; i++) {
      var b = bytes[i];
      for (var bit = 7; bit >= 0; bit--) {
        var v = (b >> bit) & 1;
        ones += v;
        if (prev !== -1 && v !== prev) runs++;
        prev = v;
      }
    }
    var zeros = n - ones;
    if (ones === 0 || zeros === 0) return NaN;
    var mu = (2 * ones * zeros) / n + 1;
    var varR = (2 * ones * zeros * (2 * ones * zeros - n)) / (n * n * (n - 1));
    if (varR <= 0) return NaN;
    return (runs - mu) / Math.sqrt(varR);
  }

  function sample() {
    var bytes = new Uint8Array(SAMPLE_BYTES);
    try {
      window.crypto.getRandomValues(bytes);
    } catch (e) {
      stop();
      setStatus('rng-status', 'CRYPTO UNAVAILABLE — getRandomValues failed', 'bad', 'bad');
      return;
    }
    var chi2 = chiSquareOf(bytes);
    var p = chiSquareP(chi2, 255);
    var z = runsTestZ(bytes);

    state.rng.count++;
    state.rng.p = p;
    state.rng.chi2 = chi2;
    state.rng.runsZ = z;
    pHist.push(p);
    if (pHist.length > HIST_MAX) pHist.shift();

    setText('rng-count', String(state.rng.count));
    setText('rng-p', 'p = ' + p.toExponential(2));
    setText('rng-chi', fmt(chi2, 1));
    setText('rng-runs', isNaN(z) ? '—' : fmt(z, 2));
    var fill = $('rng-p-fill');
    if (fill) {
      /* gauge: -log10(p) mapped 0..4 -> 0..100% (worse = longer bar) */
      var score = Math.max(0, Math.min(4, -Math.log10(Math.max(p, 1e-12)))) / 4;
      fill.style.width = (score * 100) + '%';
      fill.classList.toggle('hot', p < 0.001);
    }

    if (p < 0.001) {
      state.rng.flagged = true;
      state.rng.flagCount++;
      setStatus('rng-flag', 'FLAG — p=' + p.toExponential(2) +
        ' < 0.001 (screening trigger only)', 'bad', 'bad');
      raiseFlag('rng:p' + state.rng.flagCount,
        state.rng.flagCount >= 2 ? 't2-rng' : 't1-rng',
        'FLAG', 'RNG chi-square p=' + p.toExponential(2) +
        ' < 0.001 (χ²=' + fmt(chi2, 1) + ', df=255, event #' + state.rng.flagCount + ')');
      markTab('rng');
    } else if (state.rng.flagged && p > 0.01) {
      state.rng.flagged = false;
      setStatus('rng-flag', 'RECOVERED — p back above 0.01; flag event remains journaled', 'ok', 'on');
    }
  }

  function start() {
    if (!(window.crypto && window.crypto.getRandomValues)) {
      setStatus('rng-status', 'CRYPTO UNAVAILABLE — window.crypto.getRandomValues missing', 'bad', 'bad');
      return;
    }
    if (timer) return;
    state.rng.running = true;
    $('btn-rng-start').disabled = true;
    $('btn-rng-stop').disabled = false;
    setStatus('rng-status', 'STREAMING — 256 B samples every 500 ms', 'ok', 'on');
    timer = setInterval(sample, SAMPLE_MS);
  }

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
    state.rng.running = false;
    $('btn-rng-start').disabled = false;
    $('btn-rng-stop').disabled = true;
    setStatus('rng-status', 'IDLE — stream stopped', '', '');
  }

  /* p-history strip chart, log10 scale, threshold line at 0.001 */
  function draw() {
    if (state.activeTab !== 'rng') return;
    var cv = $('cv-rng');
    if (!cv) return;
    var ctx2 = cv.getContext('2d');
    var W = cv.width, H = cv.height;
    ctx2.fillStyle = '#101315';
    ctx2.fillRect(0, 0, W, H);

    function yOf(p) {
      var lp = Math.max(-4, Math.min(0, Math.log10(Math.max(p, 1e-12))));
      return 4 + ((-lp) / 4) * (H - 8); /* p=1 at top, p=1e-4 at bottom */
    }

    /* decade grid */
    ctx2.strokeStyle = 'rgba(74,125,120,0.18)';
    ctx2.fillStyle = 'rgba(154,149,140,0.7)';
    ctx2.font = '9px monospace';
    ctx2.textAlign = 'left';
    for (var e = 0; e >= -4; e--) {
      var y = yOf(Math.pow(10, e));
      ctx2.beginPath(); ctx2.moveTo(0, y); ctx2.lineTo(W, y); ctx2.stroke();
      ctx2.fillText('1e' + e, 4, y - 2);
    }
    /* 0.001 threshold */
    ctx2.strokeStyle = 'rgba(201,106,82,0.7)';
    ctx2.setLineDash([4, 3]);
    ctx2.beginPath(); ctx2.moveTo(0, yOf(0.001)); ctx2.lineTo(W, yOf(0.001)); ctx2.stroke();
    ctx2.setLineDash([]);

    if (pHist.length) {
      ctx2.strokeStyle = '#d8a24a';
      ctx2.beginPath();
      for (var i = 0; i < pHist.length; i++) {
        var x = (i / (HIST_MAX - 1)) * W;
        var yy = yOf(pHist[i]);
        if (i === 0) ctx2.moveTo(x, yy); else ctx2.lineTo(x, yy);
      }
      ctx2.stroke();
    }
  }

  function init() {
    on('btn-rng-start', start);
    on('btn-rng-stop', stop);
    if (!(window.crypto && window.crypto.getRandomValues)) {
      setStatus('rng-status', 'CRYPTO UNAVAILABLE — getRandomValues missing in this WebView', 'bad', 'bad');
      var b = $('btn-rng-start'); if (b) b.disabled = true;
    }
  }

  return { init: init, draw: draw, pHist: pHist };
})();

/* ================================================================
   TIERS PANEL — OMEGA-BEACON protocol checklist.
   Manual checkboxes persist; AUTO items are also ticked by flags
   raised in MONITOR / SENSORS / RNG.
   ================================================================ */

var tiers = (function () {
  var SPEC = [
    { tier: 1, label: 'TIER 1 — threshold anomalies', items: [
      { key: 't1-spike', auto: true,
        desc: '>6 dB spike at a null frequency',
        detail: 'nulls 175 / 280 / 420 / 630 / 840 Hz over rolling median (MONITOR)' },
      { key: 't1-40hz', auto: true,
        desc: '40 Hz power >3σ above baseline near TX',
        detail: 'rolling 30 s baseline during capture (MONITOR)' },
      { key: 't1-rng', auto: true,
        desc: 'RNG chi-square p < 0.001',
        detail: 'single event (RNG panel)' },
      { key: 't1-prediction', auto: false,
        desc: 'Committed prediction validated',
        detail: 'pre-committed (hashed) dream/prediction later confirmed' },
      { key: 't1-timestamp', auto: false,
        desc: 'Timestamp anomaly',
        detail: 'clock/journal ordering inconsistency observed by operator' }
    ]},
    { tier: 2, label: 'TIER 2 — structured return', items: [
      { key: 't2-harmonic', auto: false,
        desc: 'Harmonic return 280–5740 Hz, phase-stable',
        detail: 'sustained harmonic structure correlated with TX windows' },
      { key: 't2-13hz', auto: true,
        desc: 'Coherent 13 Hz tone, drift <0.1 Hz for >10 s',
        detail: '12 s tracking window (MONITOR, resolution-limited estimate)' },
      { key: 't2-rng', auto: true,
        desc: 'RNG departure replicated',
        detail: 'second distinct p < 0.001 event in-session (RNG panel)' },
      { key: 't2-infoleak', auto: false,
        desc: 'Information-leak signature',
        detail: 'decoded content matching committed material beyond chance' }
    ]},
    { tier: 3, label: 'TIER 3 — embodied / environmental', items: [
      { key: 't3-somatic', auto: false,
        desc: 'Somatic event coincident with session',
        detail: 'operator-logged; use JOURNAL note + manual tick' },
      { key: 't3-thermal', auto: false,
        desc: 'Thermal variance',
        detail: 'ambient temperature excursion logged externally' },
      { key: 't3-vib', auto: true,
        desc: '70 / 140 Hz vibration',
        detail: 'accelerometer zero-crossing estimate, sustained (SENSORS)' }
    ]}
  ];

  var autoTicks = {};   /* key -> true once an instrument flag ticked it */

  function load() {
    try {
      var raw = storage.get(LS_TIERS);
      state.tierChecks = raw ? JSON.parse(raw) : {};
    } catch (e) { state.tierChecks = {}; }
  }

  function persist() {
    try { storage.set(LS_TIERS, JSON.stringify(state.tierChecks)); } catch (e) {}
  }

  function render() {
    var host = $('tier-list');
    if (!host) return;
    host.innerHTML = '';
    SPEC.forEach(function (group) {
      var g = document.createElement('div');
      g.className = 'tier-group';
      var h = document.createElement('h3');
      h.textContent = group.label;
      g.appendChild(h);
      group.items.forEach(function (item) {
        var row = document.createElement('label');
        row.className = 'tier-item';
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = !!state.tierChecks[item.key];
        cb.setAttribute('data-key', item.key);
        cb.addEventListener('change', function () {
          state.tierChecks[item.key] = cb.checked;
          persist();
          updateBanner();
          if (cb.checked) {
            journal.add('CHECK', 'operator checked tier item: ' + item.desc);
          }
        });
        var desc = document.createElement('span');
        desc.className = 'desc';
        desc.innerHTML = escHtml(item.desc) + '<small>' + escHtml(item.detail) + '</small>';
        row.appendChild(cb);
        row.appendChild(desc);
        if (item.auto) {
          var tag = document.createElement('span');
          tag.className = 'auto';
          tag.textContent = 'AUTO';
          row.appendChild(tag);
        }
        g.appendChild(row);
      });
      host.appendChild(g);
    });
    updateBanner();
  }

  /* instrument flag -> tick a tier item (latching; journal keeps the event) */
  function autoTick(key) {
    if (autoTicks[key]) return;
    autoTicks[key] = true;
    if (!state.tierChecks[key]) {
      state.tierChecks[key] = true;
      persist();
      render();
      markTab('tiers');
    } else {
      updateBanner();
    }
  }

  function tierCounts(tierNo) {
    var met = 0, total = 0;
    SPEC.forEach(function (g) {
      if (g.tier !== tierNo) return;
      g.items.forEach(function (it) {
        total++;
        if (state.tierChecks[it.key]) met++;
      });
    });
    return { met: met, total: total };
  }

  function updateBanner() {
    var b = $('tier-banner');
    if (!b) return;
    var c1 = tierCounts(1), c2 = tierCounts(2), c3 = tierCounts(3);
    var cls = '', msg;
    if (c3.met > 0) { cls = 'l3'; msg = 'TIER-3 CRITERIA ACTIVE'; }
    else if (c2.met > 0) { cls = 'l2'; msg = 'TIER-2 CRITERIA ACTIVE'; }
    else if (c1.met > 0) { cls = 'l1'; msg = 'TIER-1 CRITERIA ACTIVE'; }
    else { msg = 'NO TIER CRITERIA MET'; }
    b.className = 'banner' + (cls ? ' ' + cls : '');
    b.textContent = 'COMPOSITE STATUS: ' + msg +
      '  ·  T1 ' + c1.met + '/' + c1.total +
      '  ·  T2 ' + c2.met + '/' + c2.total +
      '  ·  T3 ' + c3.met + '/' + c3.total;
  }

  function init() { load(); render(); }

  return { init: init, autoTick: autoTick, updateBanner: updateBanner, SPEC: SPEC };
})();

/* ================================================================
   JOURNAL PANEL UI — notes, pre-committed predictions, verify, export
   ================================================================ */

var journalUi = (function () {

  function init() {
    on('btn-jr-note', function () {
      var ta = $('jr-note-text');
      var text = ta.value.trim();
      if (!text) return;
      journal.add('NOTE', text);
      ta.value = '';
    });

    /* pre-commitment flow: hash computed and shown BEFORE saving */
    var pendingHash = null;
    on('btn-jr-hash', function () {
      var ta = $('jr-pred-text');
      var text = ta.value.trim();
      if (!text) return;
      setText('jr-pred-hash', 'computing…');
      sha256Hex('ARS-PREDICTION:' + text).then(function (h) {
        pendingHash = h;
        setText('jr-pred-hash', h);
        $('btn-jr-commit').disabled = false;
        $('btn-jr-hash-copy').disabled = false;
      });
    });
    on('btn-jr-hash-copy', function () {
      if (pendingHash) copyText(pendingHash, 'btn-jr-hash-copy');
    });
    on('btn-jr-commit', function () {
      var ta = $('jr-pred-text');
      var text = ta.value.trim();
      if (!text || !pendingHash) return;
      journal.add('PREDICTION', text + ' [pre-commit sha256: ' + pendingHash + ']');
      ta.value = '';
      pendingHash = null;
      setText('jr-pred-hash', '— committed');
      $('btn-jr-commit').disabled = true;
      $('btn-jr-hash-copy').disabled = true;
    });

    on('btn-jr-verify', function () {
      setStatus('jr-verify-status', 'VERIFYING…', 'warn', 'warn');
      journal.verify().then(function (res) {
        if (!state.journal.length) {
          setStatus('jr-verify-status', 'EMPTY — nothing to verify', '', '');
        } else if (res.ok) {
          setStatus('jr-verify-status', 'INTEGRITY PASS — ' + res.checked +
            ' entries, chain intact', 'ok', 'on');
        } else {
          setStatus('jr-verify-status', 'INTEGRITY FAIL — mismatch at entry index ' +
            res.failAt, 'bad', 'bad');
        }
      });
    });

    on('btn-jr-export', function () {
      $('jr-export').value = JSON.stringify(exportBundle(false), null, 2);
    });
    on('btn-jr-copy', function () {
      var ta = $('jr-export');
      if (ta.value) copyText(ta.value, 'btn-jr-copy');
    });
    on('btn-jr-clear', function () {
      /* double-confirm via staged button label (no modal dialogs) */
      var b = $('btn-jr-clear');
      if (b.dataset.armed === '1') {
        journal.clear();
        b.dataset.armed = '0';
        b.textContent = 'ERASE JOURNAL';
      } else {
        b.dataset.armed = '1';
        b.textContent = 'CONFIRM ERASE?';
        setTimeout(function () { b.dataset.armed = '0'; b.textContent = 'ERASE JOURNAL'; }, 3000);
      }
    });

    on('btn-export-all', function () {
      var ta = $('jr-export');
      ta.value = JSON.stringify(exportBundle(true), null, 2);
      if (activateTab) activateTab('journal');
      journal.add('EXPORT', 'full export rendered (journal + settings)');
    });
  }

  /* single JSON bundle of journal + settings */
  function exportBundle(includeSettings) {
    var bundle = {
      app: 'ARS — Aetheric Resonance Surveyor',
      version: '5.0',
      exportedAt: utcStamp(),
      storagePersistent: storage.persistent,
      journal: state.journal
    };
    if (includeSettings) {
      bundle.settings = {
        tierChecks: state.tierChecks,
        replyTrits: state.reply.join(''),
        flags: state.flags
      };
    }
    return bundle;
  }

  return { init: init, exportBundle: exportBundle };
})();

/* ================================================================
   BOOT
   ================================================================ */

function boot() {
  journal.load();
  journal.render();
  activateTab = initTabs();
  startHeaderClock();
  tiers.init();
  beacon.init();
  monitor.init();
  sensors.init();
  rng.init();
  journalUi.init();

  /* automation / test hooks */
  window.ARS = {
    state: state,
    api: {
      sha256: sha256Hex,
      sha256Sync: sha256HexSync,
      pearson: pearson,
      chiSquareP: chiSquareP,
      gammaQ: gammaQ,
      encodeWav: encodeWavPCM16,
      journalAdd: journal.add,
      journalVerify: journal.verify,
      tierAutoTick: tiers.autoTick,
      exportAll: journalUi.exportBundle,
      constants: {
        freqs: OMEGA_FREQS, amps: OMEGA_AMPS, nulls: NULL_FREQS,
        replyLevels: REPLY_LEVELS, toneSeconds: TONE_SECONDS
      }
    }
  };

  journal.add('SESSION', 'ARS v5.0 booted (storage ' +
    (storage.persistent ? 'persistent' : 'volatile') + ')');
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}

})();
