'use strict';
const crypto = require('crypto');
const fs = require('fs');
const key = crypto.createPrivateKey(process.env.MARKET_SIGNING_KEY || '');
if (key.asymmetricKeyType !== 'rsa' || key.asymmetricKeyDetails.modulusLength < 3072) {
    throw new Error('A dedicated RSA market signing key of at least 3072 bits is required');
}
process.stdout.write(crypto.sign('RSA-SHA256', fs.readFileSync(0), key).toString('base64'));
