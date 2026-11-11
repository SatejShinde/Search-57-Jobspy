const test=require('node:test');const assert=require('node:assert/strict');
const {seal,open}=require('../artifact_cipher.js');
test('listing artifact ciphertext round trips unicode without plaintext disclosure',()=>{
 const text='Fixture source body C++ \u{1F9EA}';const e=seal(Buffer.from(text),'fixture-key');
 assert.ok(!e.includes(text));assert.equal(open(e,'fixture-key').toString(),text);
});
test('listing artifact rejects wrong keys and tampering',()=>{
 const e=JSON.parse(seal(Buffer.from('fixture'),'fixture-key'));
 assert.throws(()=>open(e,'wrong'));e.tag=Buffer.alloc(16).toString('base64');assert.throws(()=>open(e,'fixture-key'));
});
test('listing encryption refuses a missing credential',()=>{
 const old=process.env.JOB_ARCHIVE_SSH_KEY;delete process.env.JOB_ARCHIVE_SSH_KEY;
 try{assert.throws(()=>seal(Buffer.from('fixture')));}finally{if(old!==undefined)process.env.JOB_ARCHIVE_SSH_KEY=old;}
});

