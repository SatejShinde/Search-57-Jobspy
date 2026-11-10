const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const restore=require('../restore_listing_artifacts');

async function fixture(fn) {
  const previous=process.cwd();const root=fs.mkdtempSync(path.join(os.tmpdir(),'jobspy-artifact-test-'));
  process.chdir(root);
  try {await fn(root);} finally {
    process.chdir(previous);
    assert.equal(path.dirname(path.resolve(root)),path.resolve(os.tmpdir()));
    assert.ok(path.basename(root).startsWith('jobspy-artifact-test-'));
    fs.rmSync(root,{recursive:true,force:true});
  }
}
function context(queue,options={}) {
  fs.writeFileSync('pending_jobspy.json',JSON.stringify(queue));
  const calls=[];const logs=[];
  return {calls,logs,args:{context:{repo:{owner:'fixture',repo:'jobspy'}},core:{info:v=>logs.push(v),warning:v=>logs.push(v)},
    github:{rest:{actions:{
      listWorkflowRunArtifacts:async args=>{calls.push(args);if(options.fail)throw Error('blocked');return {data:{artifacts:options.expired?[{id:1,expired:true}]:[{id:1,expired:false}]}};},
      downloadArtifact:async()=>({data:Buffer.from('MOCK ZIP')})
    }}},exec:()=>JSON.stringify(options.payload || {one:'Full source text'})}};
}

test('origin-run payload survives a newer watcher event; no bodies in pending metadata',async()=>fixture(async()=>{
  process.env.JOBSPY_DESCRIPTIONS_ENABLED='true';
  const f=context({one:{want_description:true,listing_run_id:'123',last_attempt:0}});
  await restore(f.args);
  assert.equal(f.calls[0].run_id,123);
  assert.equal(JSON.parse(fs.readFileSync('.runtime/jobspy-listings.json')).one,'Full source text');
  assert.ok(!fs.readFileSync('pending_jobspy.json','utf8').includes('Full source text'));
}));
test('expired or failed artifact access falls back without throwing',async()=>fixture(async()=>{
  process.env.JOBSPY_DESCRIPTIONS_ENABLED='true';
  for (const options of [{expired:true},{fail:true}]) {
    const f=context({one:{want_description:true,listing_run_id:'123'}},options);
    await restore(f.args);assert.ok(!fs.existsSync('.runtime/jobspy-listings.json'));
  }
}));
test('artifact restoration is bounded and ignores invalid run IDs',async()=>fixture(async()=>{
  process.env.JOBSPY_DESCRIPTIONS_ENABLED='true';
  const q={};for(let i=0;i<20;i++)q['key'+i]={want_description:true,listing_run_id:String(i+100)};
  q.bad={want_description:true,listing_run_id:'../../private'};
  const f=context(q);await restore(f.args);assert.equal(f.calls.length,4);
}));
test('disabled descriptions perform no artifact network calls',async()=>fixture(async()=>{
  process.env.JOBSPY_DESCRIPTIONS_ENABLED='false';
  const f=context({one:{want_description:true,listing_run_id:'123'}});await restore(f.args);assert.equal(f.calls.length,0);
}));
