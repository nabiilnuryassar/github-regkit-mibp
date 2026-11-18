// Which variant actually returns login_risk_state?
//   A: POST /console/auth/risk-context                (what line 691 does)
//   B: POST /console/auth/risk-context?platform=website (what the browser does, entry 144)
// Both carry the same headers the script sends (X-Device-Token, X-Domain, ...).
const axios = require('axios');
const { HttpsProxyAgent } = require('https-proxy-agent');

const PROXY = process.env.PROXY_URL || 'http://127.0.0.1:8888';
const UA = 'Mozilla/5.0 (X11; Linux x86_64; rv:151.0) Gecko/20100101 Firefox/151.0';
const DEVICE_TOKEN = process.env.DEVICE_TOKEN || process.env.DEVICE_TOKEN || '';

function headers(referer) {
  return {
    Host: 'www.codebuddy.ai',
    'Sec-Ch-Ua-Platform': '"Linux"',
    'Accept-Language': 'en-US,en;q=0.9',
    'Sec-Ch-Ua': '"Chromium";v="151", "Not=A?Brand";v="99"',
    'Sec-Ch-Ua-Mobile': '?0',
    'X-Device-Token': DEVICE_TOKEN,
    'X-Domain': 'www.codebuddy.ai',
    'X-Requested-With': 'XMLHttpRequest',
    'User-Agent': UA,
    Accept: 'application/json, text/plain, */*',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Dest': 'empty',
    Referer: referer,
    'Accept-Encoding': 'gzip, deflate, br',
    Priority: 'u=1, i',
  };
}

async function hit(label, url, referer) {
  const agent = new HttpsProxyAgent(PROXY);
  try {
    const r = await axios.post(url, '', {
      headers: headers(referer),
      timeout: 60000,
      maxRedirects: 0,
      validateStatus: () => true,
      httpsAgent: agent,
      httpAgent: agent,
    });
    const sc = r.headers['set-cookie'];
    const n = (Array.isArray(sc) ? sc : sc ? [sc] : []).map((c) => String(c).split('=')[0]);
    console.log(`${label}`);
    console.log(`   status        : ${r.status}`);
    console.log(`   set-cookie    : ${n.join(', ') || '(none)'}`);
    console.log(`   risk-state?   : ${n.includes('login_risk_state')}`);
    console.log(`   body          : ${JSON.stringify(r.data).slice(0, 110)}`);
  } catch (e) {
    console.log(`${label}\n   ERROR: ${e.message}`);
  }
}

(async () => {
  await hit('A) no ?platform  (line 691 today)', 'https://www.codebuddy.ai/console/auth/risk-context', 'https://www.codebuddy.ai/');
  console.log('');
  await hit('B) ?platform=website (browser / line 1074)', 'https://www.codebuddy.ai/console/auth/risk-context?platform=website', 'https://www.codebuddy.ai/login/');
})();
