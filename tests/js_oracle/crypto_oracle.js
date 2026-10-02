// Reference implementation of the Curious encryption using Node's crypto.
// Applet side copies mindlogger-admin/src/shared/utils/encryption/encryption.ts;
// respondent side copies mindlogger-app-refactor/src/shared/lib/encryption/EncryptionManager.ts.
// Reads a JSON job on stdin and prints the results as JSON.
const crypto = require('crypto');

const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const { prime, base, appletPassword, accountId, respondent, plaintexts } = input;

// admin getPrivateKey: template literal turns both digests into strings
const key1 = crypto.createHash('sha512').update(appletPassword).digest();
const key2 = crypto.createHash('sha512').update(accountId).digest();
const appletPrivateKey = Buffer.from(`${key1}${key2}`);

// admin getAppletEncryptionInfo: public key = g^priv mod p
const applet = crypto.createDiffieHellman(Buffer.from(prime), Buffer.from(base));
applet.setPrivateKey(appletPrivateKey);
applet.generateKeys();
const appletPublicKey = Array.from(applet.getPublicKey());

// app getPrivateKey (respondent): Buffer.concat, no string conversion
const left = crypto.createHash('sha512').update(respondent.password + respondent.email).digest();
const right = crypto.createHash('sha512').update(respondent.userId + respondent.email).digest();
const userPrivateKey = Array.from(Buffer.concat([left, right]));

// app getPublicKey
const user = crypto.createDiffieHellman(Buffer.from(prime), Buffer.from(base));
user.setPrivateKey(Buffer.from(userPrivateKey));
user.generateKeys();
const userPublicKey = Array.from(user.getPublicKey());

// app getAESKey
const dh = crypto.createDiffieHellman(Buffer.from(prime), Buffer.from(base));
dh.setPrivateKey(Buffer.from(userPrivateKey));
const secret = dh.computeSecret(Buffer.from(appletPublicKey));
const aesKey = crypto.createHash('sha256').update(secret).digest();

// app encryptData
const ciphertexts = plaintexts.map((text) => {
  const iv = crypto.randomBytes(16);
  const cipher = crypto.createCipheriv('aes-256-cbc', aesKey, iv);
  const encrypted = Buffer.concat([cipher.update(text), cipher.final()]);
  return `${iv.toString('hex')}:${encrypted.toString('hex')}`;
});

process.stdout.write(
  JSON.stringify({
    appletPrivateKeyHex: appletPrivateKey.toString('hex'),
    appletPublicKey,
    userPublicKey,
    secretLength: secret.length,
    ciphertexts,
  }),
);
