// Does POST /console/auth/risk-context actually return login_risk_state
// when called the way github-login.js calls it?
const axios = require('axios');
const { HttpsProxyAgent } = require('https-proxy-agent');

const PROXY = process.env.PROXY_URL || 'http://127.0.0.1:8888';
const DEVICE_TOKEN = process.env.DEVICE_TOKEN || process.env.DEVICE_TOKEN || '';
const UA = 'Mozilla/5.0 (X11; Linux x86_64; rv:151.0) Gecko/20100101 Firefox/151.0';

(async () => {
  const agent = new HttpsProxyAgent(PROXY);
  const cfg = {
    headers: {
      Host: 'www.codebuddy.ai',
      'User-Agent': UA,
      Accept: 'application/json, text/plain, */*',
      'Accept-Language': 'en-US,en;q=0.9',
      'X-Domain': 'www.codebuddy.ai',
      'X-Device-Token': DEVICE_TOKEN,
      'X-Requested-With': 'XMLHttpRequest',
      'X-Product-Code': 'workbuddy',
      Origin: 'https://www.codebuddy.ai',
      Referer: 'https://www.codebuddy.ai/login/',
      'Sec-Fetch-Site': 'same-origin',
      'Sec-Fetch-Mode': 'cors',
      'Sec-Fetch-Dest': 'empty',
    },
    timeout: 60000,
    maxRedirects: 0,
    validateStatus: () => true,
    httpsAgent: agent,
    httpAgent: agent,
  };
  const url = 'https://www.codebuddy.ai/console/auth/risk-context?platform=website';
  const res = await axios.post(url, '', cfg);
  console.log('status:', res.status);
  console.log('body  :', JSON.stringify(res.data).slice(0, 160));
  const sc = res.headers['set-cookie'];
  console.log('set-cookie raw:', sc ? sc.map((s) => s.split(';')[0]) : '(NONE)');
  if (sc) {
    for (const c of sc) {
      const m = String(c).match(/^([^=]+)=([^;]+)/);
      const attrs = String(c).slice(String(c).indexOf(';'));
      console.log(`  parsed ${m && m[1]} = ${m && m[2]}`);
      console.log(`  attrs: ${attrs}`);
    }
  }
})().catch((e) => console.log('ERROR:', e.message));
