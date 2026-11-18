// Spoof CodeBuddy's device identity by running the site's own TDID SDK in Node.
//
// The bundle carries TDID as an inline script (`Gc`) and boots it with
// `_TDID.init({channel: "400101"})`; `getDeviceTokenInfoV3` then returns the
// `v3:…` string the risk service expects. Running that same source here — instead of
// the fake `v3:<base64>#0#<base64>` the reference repo invents — is what makes a
// device identity that the site can verify.
//
// What this file does:
//   1. pulls `Gc` out of the bundle (the template literal between `Gc=` and
//      `],void 0)();;`)
//   2. builds just enough of a browser (document/navigator/window/canvas/WebRTC
//      stubs) for the SDK to run
//   3. seeds a machine id per account — stable for the account, different on retry
//      (the `_device_seed`/`generation` shape ConTools uses for Qoder)
//   4. prints every token the SDK returns, per seed
//
// Usage:
//   node /tmp/tdid_spoof.js --bundle /tmp/cb_main.js --email a@b.com
//   node /tmp/tdid_spoof.js --bundle /tmp/cb_main.js --email a@b.com --gen 1
'use strict';

const fs = require('fs');
const crypto = require('crypto');
const vm = require('vm');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : dflt;
}

const BUNDLE = arg('bundle', '/tmp/cb_main.js');
const EMAIL = arg('email', '');
const GEN = parseInt(arg('gen', '0'), 10);
const CHANNEL = arg('channel', '400101');

// ── 1. the SDK source ────────────────────────────────────────────────────
function extractGc(path) {
  const s = fs.readFileSync(path, 'utf8');
  const mark = '],void 0)();;';
  const end = s.indexOf(mark);
  if (end < 0) throw new Error('TDID end marker not found');
  const stop = end + mark.length;
  // The template literal closes with a backtick immediately after the marker; the
  // opening one is the *first* backtick after `Gc=`, not the nearest one before.
  const assign = s.lastIndexOf('Gc=', end);
  if (assign < 0) throw new Error('Gc= assignment not found');
  const open = s.indexOf('`', assign);
  if (open < 0 || open > end) throw new Error('opening backtick not found before marker');
  const out = s.slice(open + 1, stop);
  if (!out) throw new Error(`empty slice: open=${open} stop=${stop}`);
  return out;
}

// ── 2. a device seed: stable per account, different per retry ────────────
function deviceSeed(email, generation) {
  const key = (email || '').trim().toLowerCase() || `anon-${crypto.randomUUID()}`;
  return `cb-device:${key}${generation > 0 ? `:gen${generation}` : ''}`;
}

function seedValues(seed) {
  const h = (tag) => crypto.createHash('sha256').update(seed + tag).digest('hex');
  const h1 = h('_machine');
  return {
    machineId: h1.slice(0, 32),
    osVersion: '10.0.0',
    ua: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      + '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36',
  };
}

// ── 3. the smallest browser the SDK needs ────────────────────────────────
function makeBrowser(seedInfo) {
  const noop = () => {};
  const listeners = new Map();

  const canvas = {
    width: 300, height: 150,
    getContext: () => ({
      fillRect: noop, fillText: noop, measureText: () => ({ width: 42 }),
      getImageData: (x, y, w, hh) => ({
        width: w, height: hh,
        data: new Uint8ClampedArray(w * hh * 4).map((_, i) => (i * 7 + 13) % 256),
      }),
      putImageData: noop, drawImage: noop, beginPath: noop, arc: noop,
      fill: noop, stroke: noop, save: noop, restore: noop, translate: noop,
      rotate: noop, scale: noop, createLinearGradient: () => ({ addColorStop: noop }),
      createRadialGradient: () => ({ addColorStop: noop }),
      getContextAttributes: () => ({}),
      canvas: null,
    }),
    toDataURL: () => 'data:image/png;base64,' + Buffer.from(seedInfo.machineId).toString('base64'),
    addEventListener: noop,
  };

  const element = () => ({
    style: {}, appendChild: noop, removeChild: noop, setAttribute: noop,
    getAttribute: () => null, addEventListener: noop, removeEventListener: noop,
    getContext: canvas.getContext, width: 0, height: 0,
  });

  const documentStub = {
    createElement: (tag) => (tag === 'canvas' ? { ...canvas } : element()),
    createElementNS: () => element(),
    getElementsByTagName: () => [element()],
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: noop,
    removeEventListener: noop,
    head: element(),
    body: element(),
    documentElement: { style: {}, setAttribute: noop },
    cookie: '',
    readyState: 'complete',
    hidden: false,
    visibilityState: 'visible',
    hasFocus: () => true,
  };

  const navigatorStub = {
    userAgent: seedInfo.ua,
    platform: 'Win32',
    language: 'en-US',
    languages: ['en-US', 'en'],
    hardwareConcurrency: 8,
    deviceMemory: 8,
    maxTouchPoints: 0,
    webdriver: false,
    cookieEnabled: true,
    plugins: [{ name: 'PDF Viewer' }, { name: 'Chrome PDF Viewer' }],
    mimeTypes: [{ type: 'application/pdf' }],
    mediaDevices: undefined,          // no camera/mic: a datacenter device
    getBattery: () => Promise.resolve({ charging: true, level: 1 }),
    sendBeacon: () => true,
  };

  const storage = new Map();
  const localStorage = {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
    clear: () => storage.clear(),
    key: (i) => Array.from(storage.keys())[i] || null,
    get length() { return storage.size; },
  };

  const windowStub = {
    document: documentStub,
    navigator: navigatorStub,
    location: {
      href: 'https://www.codebuddy.ai/login?platform=website',
      origin: 'https://www.codebuddy.ai',
      protocol: 'https:', host: 'www.codebuddy.ai',
      hostname: 'www.codebuddy.ai', pathname: '/login',
      search: '?platform=website', hash: '',
    },
    screen: { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040, colorDepth: 24 },
    innerWidth: 1920, innerHeight: 1080,
    devicePixelRatio: 1,
    localStorage, sessionStorage: localStorage,
    setTimeout, clearTimeout, setInterval, clearInterval,
    queueMicrotask, Promise, Math, Date, JSON, console,
    fetch: globalThis.fetch,
    XMLHttpRequest: undefined,
    crypto: { getRandomValues: (a) => crypto.webcrypto.getRandomValues(a), randomUUID: () => crypto.randomUUID() },
    addEventListener: (name, fn) => { listeners.set(name, fn); },
    removeEventListener: noop,
    performance: { now: () => Date.now() },
    matchMedia: () => ({ matches: false, addListener: noop, removeListener: noop }),
    requestAnimationFrame: (fn) => setTimeout(fn, 16),
    RTCPeerConnection: undefined,     // no WebRTC on a server
    webkitRTCPeerConnection: undefined,
    AudioContext: undefined,
    OffscreenCanvas: undefined,
    WebGLRenderingContext: undefined,
    chrome: undefined,
  };
  windowStub.window = windowStub;
  windowStub.self = windowStub;
  windowStub.top = windowStub;
  windowStub.parent = windowStub;
  windowStub.globalThis = windowStub;
  return windowStub;
}

// ── 4. run the SDK ──────────────────────────────────────────────────────
async function main() {
  const gc = extractGc(BUNDLE);
  console.log(`SDK source: ${gc.length} chars from ${BUNDLE}`);

  const seed = deviceSeed(EMAIL, GEN);
  const info = seedValues(seed);
  console.log(`seed: ${seed}`);
  console.log(`machineId: ${info.machineId}`);

  const sandbox = makeBrowser(info);
  const ctx = vm.createContext(sandbox);

  try {
    // The template literal in the bundle escapes its inner script; decode it the way
    // a template literal would before handing the source to V8.
    const decoded = typeof gc === 'string'
      ? gc.replace(/\\`/g, '`').replace(/\\\$/g, '$')
      : gc;
    vm.runInContext(decoded, ctx, { filename: 'tdid-sdk.js', timeout: 30_000 });
    console.log(`SDK loaded (${decoded.length} chars)`);
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    console.log(`SDK did not load: ${msg}`);
    const at = /tdid-sdk\.js:(\d+)/.exec(e && e.stack ? e.stack : '');
    if (at) {
      const lineNo = parseInt(at[1], 10);
      const lines = gc.split('\n');
      for (let k = Math.max(0, lineNo - 3); k < Math.min(lines.length, lineNo + 2); k++) {
        console.log(`  ${k + 1}| ${lines[k].slice(0, 160)}`);
      }
    }
    return 1;
  }
  const TDID = sandbox._TDID || ctx._TDID;
  if (!TDID) {
    console.log('_TDID missing after the SDK ran — the source needs a signal this stub does not provide');
    return 1;
  }
  console.log('_TDID loaded. methods:', Object.keys(TDID).join(', '));

  const initRet = await new Promise((resolve) => {
    try {
      TDID.init({ channel: CHANNEL }, (r) => resolve(r));
      setTimeout(() => resolve({ ret: 'timeout' }), 20_000);
    } catch (e) { resolve({ ret: 'throw', err: String(e) }); }
  });
  console.log('init ->', JSON.stringify(initRet));

  const tok = await new Promise((resolve) => {
    try {
      TDID.getDeviceTokenInfoV3((g) => resolve(g), true);
      setTimeout(() => resolve({ ret: 'timeout' }), 30_000);
    } catch (e) { resolve({ ret: 'throw', err: String(e) }); }
  });
  console.log('getDeviceTokenInfoV3 ->', JSON.stringify(tok).slice(0, 600));

  if (tok && tok.deviceTokenInfo) {
    const out = `/tmp/tdid_token_${(EMAIL || 'anon').replace(/[^a-z0-9]/gi, '_')}_gen${GEN}.txt`;
    fs.writeFileSync(out, tok.deviceTokenInfo + '\n');
    console.log(`\nTOKEN written to ${out} (${tok.deviceTokenInfo.length} chars)`);
    return 0;
  }
  console.log('\nno token — see the ret/err above');
  return 1;
}

main().then((c) => process.exit(c)).catch((e) => {
  console.log('fatal:', e && e.stack ? e.stack.split('\n').slice(0, 4).join('\n') : e);
  process.exit(1);
});
