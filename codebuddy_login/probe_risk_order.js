// Hypothesis: /console/auth/risk-context only returns login_risk_state when it is
// called AFTER a `session` cookie exists (browser order: /console/accounts -> session,
// then risk-context). github-login.js calls risk-context FIRST (CB-0pre), before any
// session, which is why login_risk_state never appears in our runs.
const axios = require('axios');
const { HttpsProxyAgent } = require('https-proxy-agent');

const PROXY = process.env.PROXY_URL || 'http://127.0.0.1:8888';
const UA = 'Mozilla/5.0 (X11; Linux x86_64; rv:151.0) Gecko/20100101 Firefox/151.0';
const DEVICE_TOKEN = process.env.DEVICE_TOKEN || process.env.DEVICE_TOKEN || '';

const base = {
  headers: {
    Host: 'www.codebuddy.ai',
    'User-Agent': UA,
    Accept: 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'X-Domain': 'www.codebuddy.ai',
    'X-Device-Token': DEVICE_TOKEN,
    'X-Product-Code': 'workbuddy',
    'X-Requested-With': 'XMLHttpRequest',
    Origin: 'https://www.codebuddy.ai',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Dest': 'empty',
  },
  timeout: 60000,
  maxRedirects: 0,
  validateStatus: () => true,
  httpsAgent: new HttpsProxyAgent(PROXY),
  httpAgent: new HttpsProxyAgent(PROXY),
};

function names(sc) {
  if (!sc) return [];
  return (Array.isArray(sc) ? sc : [sc]).map((c) => String(c).split('=')[0]);
}

async function call(path, method, cookie, headers = {}) {
  const cfg = { ...base, headers: { ...base.headers, ...headers } };
  if (cookie) cfg.headers.Cookie = cookie;
  const url = 'https://www.codebuddy.ai' + path;
  return method === 'POST' ? axios.post(url, '', cfg) : axios.get(url, cfg);
}

(async () => {
  console.log('--- A: risk-context with NO session (what the script does today) ---');
  let r = await call('/console/auth/risk-context?platform=website', 'POST', '');
  console.log('status', r.status, '| set-cookie names:', names(r.headers['set-cookie']));
  console.log('has login_risk_state:', names(r.headers['set-cookie']).includes('login_risk_state'));

  console.log('\n--- B: get a session first, then risk-context (browser order) ---');
  const jar = {};
  const collect = (sc) => {
    if (!sc) return;
    for (const c of (Array.isArray(sc) ? sc : [sc])) {
      const m = String(c).match(/^([^=]+)=([^;]+)/);
      if (m) jar[m[1]] = m[2];
    }
  };
  const cookieStr = () => Object.entries(jar).map(([k, v]) => `${k}=${v}`).join('; ');

  r = await call('/console/accounts', 'GET', cookieStr(), {
    Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    Referer: 'https://www.codebuddy.ai/',
  });
  collect(r.headers['set-cookie']);
  console.log('GET /console/accounts ->', r.status, '| jar:', Object.keys(jar));

  r = await call('/console/auth/risk-context?platform=website', 'POST', cookieStr());
  collect(r.headers['set-cookie']);
  console.log('POST risk-context     ->', r.status, '| set-cookie names:', names(r.headers['set-cookie']));
  console.log('has login_risk_state:', names(r.headers['set-cookie']).includes('login_risk_state'));
  console.log('body:', JSON.stringify(r.data).slice(0, 140));
})().catch((e) => console.log('ERROR:', e.message));
