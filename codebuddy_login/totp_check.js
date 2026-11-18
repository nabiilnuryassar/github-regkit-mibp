// sanity check: every secret in codebuddy.txt must produce a 6-digit code
// totp-generator v2 exposes a static async `TOTP.generate(secret)`.
const { TOTP } = require('totp-generator');
const fs = require('fs');

(async () => {
  const lines = fs.readFileSync('codebuddy.txt', 'utf8').trim().split('\n');
  for (const line of lines) {
    const p = line.split('|');
    try {
      const { otp } = await TOTP.generate(p[3], { explicitZeroPad: true });
      console.log(p[0].slice(0, 32), '-> code', otp);
    } catch (e) {
      console.log(p[0].slice(0, 32), '-> ERROR', e.message);
    }
  }
})();
