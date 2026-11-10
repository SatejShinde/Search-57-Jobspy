// Bounded, temporary recovery only; no descriptions enter Git or logs.
module.exports = async ({github, context, core, exec = require('node:child_process').execFileSync}) => {
  const fs = require('node:fs');
  const queue = JSON.parse(fs.readFileSync('pending_jobspy.json', 'utf8'));
  const enabled = process.env.JOBSPY_DESCRIPTIONS_ENABLED === 'true';
  if (!enabled) return;
  const entries = Object.entries(queue).filter(([,e]) => e.want_description)
    .sort((a,b) => (a[1].last_attempt || 0)-(b[1].last_attempt || 0)).slice(0,12);
  const runs = [...new Set(entries.map(([,e])=>e.listing_run_id).filter(id=>/^\d+$/.test(id || '')))].slice(0,4);
  const cache={}; const started=Date.now(); fs.mkdirSync('.runtime',{recursive:true});
  for (const run_id of runs) {
    if (Date.now()-started>40000) break;
    const zip=`.runtime/listing-${run_id}.zip`;
    try {
      const result=await github.rest.actions.listWorkflowRunArtifacts({owner:context.repo.owner,
        repo:context.repo.repo,run_id:Number(run_id),name:'jobspy-listing-descriptions',request:{timeout:8000}});
      const artifact=result.data.artifacts.find(a=>!a.expired);
      if (!artifact) continue;
      const response=await github.rest.actions.downloadArtifact({owner:context.repo.owner,
        repo:context.repo.repo,artifact_id:artifact.id,archive_format:'zip',request:{timeout:8000}});
      fs.writeFileSync(zip,Buffer.from(response.data));
      // No archive extraction: read the one expected file, rejecting other data.
      const value=JSON.parse(exec('unzip',['-p',zip,'jobspy-listings.json'],
        {encoding:'utf8',timeout:5000,maxBuffer:16*1024*1024}));
      for (const [key,entry] of entries) {
        if (entry.listing_run_id===run_id && typeof value[key]==='string') cache[key]=value[key];
      }
      fs.writeFileSync('.runtime/jobspy-listings.json.tmp',JSON.stringify(cache));
      fs.renameSync('.runtime/jobspy-listings.json.tmp','.runtime/jobspy-listings.json');
    } catch {
      core.warning(`Transient listing payload unavailable for run ${run_id}; targeted fallback remains available`);
    } finally {
      if (fs.existsSync(zip)) fs.unlinkSync(zip);
    }
  }
  core.info(`Recovered transient descriptions for ${Object.keys(cache).length} pending job(s)`);
};
