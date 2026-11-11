// Authenticated encryption for pre-existing transient listing artifacts only.
// The repository-scoped archive key never enters an artifact or log.
const crypto=require('node:crypto');
const fs=require('node:fs');
function key(secret=process.env.JOB_ARCHIVE_SSH_KEY) {
  if(!secret)throw Error('Archive encryption credential unavailable');
  return crypto.createHash('sha256').update(secret).digest();
}
function seal(body,secret) {
  const iv=crypto.randomBytes(12);const cipher=crypto.createCipheriv('aes-256-gcm',key(secret),iv);
  cipher.setAAD(Buffer.from('job-archive-listing-v1'));
  const data=Buffer.concat([cipher.update(body),cipher.final()]);
  return JSON.stringify({v:1,iv:iv.toString('base64'),tag:cipher.getAuthTag().toString('base64'),ciphertext:data.toString('base64')});
}
function open(envelope,secret) {
  const e=typeof envelope==='string'?JSON.parse(envelope):envelope;
  if(e.v!==1 || !e.ciphertext)throw Error('Invalid encrypted listing envelope');
  const decipher=crypto.createDecipheriv('aes-256-gcm',key(secret),Buffer.from(e.iv,'base64'));
  decipher.setAAD(Buffer.from('job-archive-listing-v1'));decipher.setAuthTag(Buffer.from(e.tag,'base64'));
  return Buffer.concat([decipher.update(Buffer.from(e.ciphertext,'base64')),decipher.final()]);
}
module.exports={seal,open};
if(require.main===module) {
  try {
    const [operation,path]=process.argv.slice(2);
    if(!fs.existsSync(path))process.exit(0);
    if(operation==='seal')fs.writeFileSync(path+'.enc',seal(fs.readFileSync(path)));
    else if(operation==='open' && path.endsWith('.enc'))fs.writeFileSync(path.slice(0,-4),open(fs.readFileSync(path,'utf8')));
    else throw Error('Invalid encryption operation');
  } catch {console.error('Encrypted listing handoff unavailable; targeted fallback remains enabled');process.exitCode=1;}
}
