'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const app = $('#app');
const state = { boot: null, runs: [], project: 'all', filter: 'all', stage: null, detail: null, reviewNote: '', signature: '', polling: false };
const statuses = { draft: ['Draft', ''], queued: ['Queued', 'blue'], running: ['Workflow running', 'blue'], awaiting_review: ['Awaiting review', 'amber'], completed: ['Completed', 'green'], cancelled: ['Cancelled', ''], failed: ['Failed', 'red'] };
const roleNames={admin:'Admin',researcher:'Researcher',reviewer:'Reviewer',viewer:'Read-only member'};
const can = permission => ({admin:['read','write','review','members'],researcher:['read','write'],reviewer:['read','review'],viewer:['read']}[state.boot?.workspace?.role]||[]).includes(permission);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const chip = (text, tone='') => `<span class="chip ${tone}">${escape(text)}</span>`;
const statusChip = status => chip(...(statuses[status] || [status, '']));
const date = value => new Date(value).toLocaleString('en-US', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});

async function api(path, data) {
  const workspace=state.boot?.workspace?.id||localStorage.getItem('atlas-workspace')||'';
  const options = {headers:{'X-Workspace-ID':workspace},credentials:'same-origin'};
  if(data!==undefined)Object.assign(options,{method:'POST',headers:{...options.headers,'Content-Type':'application/json','X-Workbench-Token':state.boot.token},body:JSON.stringify(data)});
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) {
    if(response.status===401&&!path.startsWith('/api/auth/')){state.boot=null;state.runs=[];state.detail=null;document.querySelectorAll('dialog[open]').forEach(d=>d.close());initialize();}
    const error=new Error(body.error||'Request failed');error.status=response.status;throw error;
  }
  return body;
}
function toast(message) { const el = $('#toast'); el.textContent = message; el.classList.add('visible'); clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove('visible'), 4500); }
function scopedRuns() { return state.runs.filter(r => state.project === 'all' || r.project_id === state.project); }
function projectName(id) { return state.boot.projects.find(p => p.id === id)?.name || 'Project'; }

function sidebar() {
  $('#workspace-select').innerHTML=state.boot.workspaces.map(w=>`<option value="${w.id}">${escape(w.name)}</option>`).join('');
  $('#workspace-select').value=state.boot.workspace?.id||'';
  $('#workspace-role').textContent=roleNames[state.boot.workspace?.role]||'';
  $('#user-avatar').textContent=state.boot.user.display_name.slice(0,1);
  $('#user-avatar').title=state.boot.user.display_name;
  $('#add-project').hidden=!can('write');
  $('#project-list').innerHTML = `<button class="project-item ${state.project==='all'?'selected':''}" data-project="all">All projects</button>` + state.boot.projects.map(p => `<button class="project-item ${state.project===p.id?'selected':''}" data-project="${escape(p.id)}">${escape(p.name)}</button>`).join('');
  $('#project-select').innerHTML = state.boot.projects.map(p => `<option value="${escape(p.id)}">${escape(p.name)}</option>`).join('');
  if (state.project !== 'all') $('#project-select').value = state.project;
}
function heading(eyebrow, title, description, action='') { return `<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p>${description}</p></div>${action}</div>`; }

function workspace() {
  const runs = scopedRuns();
  const filtered = runs.filter(r => state.filter==='all' || (state.filter==='active' ? ['queued','running'].includes(r.status) : r.status===state.filter));
  app.innerHTML = heading('RESEARCH WORKSPACE', 'Make research traceable', 'Coordinate specialist agents and preserve evidence at every step.', '<button class="button primary" data-create="">＋ New research</button>') +
    `<div class="stats"><div class="stat"><div class="stat-label">Research runs <span>▦</span></div><div class="stat-number">${runs.length}<small>total</small></div></div><div class="stat"><div class="stat-label">In progress <span>↻</span></div><div class="stat-number">${runs.filter(r=>['queued','running'].includes(r.status)).length}<small>planning or scientific computation</small></div></div><div class="stat"><div class="stat-label">Awaiting review <span>◷</span></div><div class="stat-number">${runs.filter(r=>r.status==='awaiting_review').length}<small>Your review needed</small></div></div><div class="stat"><div class="stat-label">Scientific adapters <span>◈</span></div><div class="stat-number">${state.boot.capabilities?.length||0}<small>available modules</small></div></div></div>` +
    `<div class="section-heading"><h2>Choose a research domain</h2><span>Shared runtime · independent domain workflows</span></div><div class="domain-grid">${state.boot.packs.map((p,i)=>`<article class="domain-card"><div class="domain-top"><span class="domain-icon ${i?'green':''}">${i?'◇':'⌬'}</span>${chip('Scientific adapters connected','blue')}</div><h3>${escape(p.name)}</h3><p>${escape(p.description)}</p><div class="domain-footer"><span>${new Set(p.stages.map(s=>s.role)).size} agent roles · ${p.stages.length} stages</span><button class="text-button" data-create="${escape(p.id)}">Create run ↗</button></div></article>`).join('')}</div>` +
    `<div class="notice"><span>ⓘ</span><span>Agent orchestration and scientific computation run locally.${state.boot.model.configured?'You can optionally enable an LLM for advice.':'No LLM is configured; scientific adapters still run on the supplied inputs.'} Missing models, datasets, or experimental inputs are reported at the relevant stage.</span></div>` +
    `<div class="section-heading"><h2>Research runs ${state.project!=='all'?`· ${escape(projectName(state.project))}`:''}</h2><div class="filter-tabs">${[['all','All'],['active','In progress'],['awaiting_review','Needs review'],['completed','Completed']].map(([id,label])=>`<button class="${state.filter===id?'selected':''}" data-filter="${id}">${label}</button>`).join('')}</div></div><div class="tasks-panel">` +
    (filtered.length ? `<div class="table-wrap"><table><thead><tr><th>Run title</th><th>Domain</th><th>Status</th><th>Created</th><th></th></tr></thead><tbody>${filtered.map(r=>`<tr tabindex="0" role="link" data-run="${r.id}" aria-label="Open ${escape(r.title)}"><td><span class="task-title">${escape(r.title)}</span><span class="task-sub">${escape(projectName(r.project_id))} · ${r.id.slice(0,8)}</span></td><td>${escape(r.pack.name)}</td><td>${statusChip(r.status)}</td><td class="muted">${date(r.created_at)}</td><td class="muted">↗</td></tr>`).join('')}</tbody></table></div>` : `<div class="empty"><div class="empty-symbol">▤</div><h3>${runs.length?'No runs match this filter':'Start your first research run here'}</h3><p>Define an objective, add inputs, and review agent outputs and open questions.</p><button class="text-button" data-create="">Create research run ↗</button></div>`) + '</div><div class="footer-note">RESEARCH ATLAS / Every conclusion has limits. Every step leaves a record.</div>';
  applyPermissions();
}

function domains() {
  const capabilityChip=c=>{if(!c)return chip('No adapter','red');const stateLabel={ready:'Engine connected',partial:'Partially computable',needs_dependency:'Missing runtime dependency'}[c.state]||'Engine connected';return chip(stateLabel,c.state==='ready'?'green':c.state==='partial'?'amber':'red');};
  app.innerHTML = heading('DOMAIN PACKS', 'Domains & Capabilities', 'See which adapters are connected, which baselines can run, and what models or experimental data are missing.') +
    `<div class="domain-grid">${state.boot.packs.map(p=>`<section class="panel"><h2>${escape(p.name)}</h2><p class="muted small">${escape(p.description)}</p>${p.stages.map((s,i)=>{const capability=state.boot.capabilities.find(c=>c.id===s.tool);return `<div class="capability"><div><strong class="small">${i+1}. ${escape(s.name)}</strong><p>${escape(s.role)}</p>${capability?`<p>${escape(capability.details||capability.requirement)}</p>`:''}</div>${s.kind==='approval'?chip('Human review','amber'):s.tool.startsWith('research.')?chip('Orchestrated','blue'):capabilityChip(capability)}</div>`;}).join('')}<button class="button primary full" data-create="${p.id}">Create run</button></section>`).join('')}</div><section class="panel"><h2>Scientific Tool Registry</h2><p class="muted small">“Engine connected” means the adapter can execute. Results still depend on inputs, models, and experimental data. Rule-based baselines do not replace calibrated models.</p>${state.boot.capabilities.map(c=>`<div class="capability"><div><strong class="small">${escape(c.name)}</strong><p>${escape(c.details||c.requirement)} · ${escape(c.id)}</p></div>${capabilityChip(c)}</div>`).join('')}</section>`;
  applyPermissions();
}

function scienceMarkup(output,run){
  const tool=output?.tool_result;if(!tool?.data&&!tool?.missing)return '';
  const status={computed:'Computation complete',partial:'Partial computation / open requirements',needs_input:'Waiting for scientific inputs',needs_dependency:'Waiting for dependencies',invalid_input:'Input needs correction',failed:'Computation failed',timeout:'Computation timed out'}[tool.status]||tool.status;
  const rows=tool.data?.molecules||tool.data?.candidates;
  const numeric=v=>typeof v==='number'&&Number.isFinite(v)?v.toFixed(3):'—';
  let html=`<div class="instruction"><h3>${escape(status)}</h3>`;
  if(Array.isArray(rows)&&rows.length)html+=`<div class="table-wrap"><table><thead><tr><th>Molecule</th><th>MW</th><th>cLogP</th><th>QED</th><th>Vina Score</th></tr></thead><tbody>${rows.slice(0,20).map(r=>`<tr><td>${escape(r.name||r.smiles)}</td><td>${numeric(r.descriptors?.mw)}</td><td>${numeric(r.descriptors?.logp)}</td><td>${numeric(r.descriptors?.qed)}</td><td>${numeric(r.docking?.scores_kcal_mol?.[0])}</td></tr>`).join('')}</tbody></table></div>`;
  if(tool.missing?.length)html+='<h3>Still needed</h3><ul>'+tool.missing.map(x=>`<li>${escape(x)}</li>`).join('')+'</ul>';
  if(tool.artifacts?.length)html+='<div class="actions">'+tool.artifacts.map(name=>`<a class="button" href="/api/runs/${run.id}/artifacts/${encodeURIComponent(name)}">↓ ${escape(name)}</a>`).join('')+'</div>';
  if(tool.data)html+=`<details><summary>View structured scientific results (first 24,000 characters)</summary><pre class="code science-json">${escape(JSON.stringify(tool.data,null,2).slice(0,24000))}</pre></details>`;
  if(tool.evidence?.length)html+=`<details><summary>Data Sources and Method Evidence</summary><pre class="code science-json">${escape(JSON.stringify(tool.evidence,null,2))}</pre></details>`;
  return html+'</div>';
}

function settings() {
  app.innerHTML = heading('LOCAL RUNTIME', 'Runtime Settings', 'Scientific adapters run locally. Missing models and experimental data are shown at each stage.') +
    `<div class="config-grid"><section class="panel"><h2>Local Runtime</h2><div class="capability"><span>Runtime</span>${chip('Local Python service','green')}</div><div class="capability"><span>Data storage</span><span class="small muted">SQLite + local files</span></div><div class="capability"><span>Access scope</span><span class="small muted">127.0.0.1 / localhost</span></div><div class="capability"><span>Background processing</span><span class="small muted">Single worker · stage checkpoints</span></div><div class="capability"><span>LLM provider</span>${chip(state.boot.model.configured?state.boot.model.name:'Not configured',state.boot.model.configured?'green':'')}</div></section><section class="panel"><h2>Configure an LLM provider</h2><p class="muted small">Supports chat-completions compatible APIs. Environment variables are read at startup; restart after changes. API keys are not stored in the database or shown in the UI.</p><pre class="code">export LLM_BASE_URL="Your API base URL (including /v1)"
export LLM_MODEL="Your model name"
export LLM_API_KEY="Your API key"
python3 -m workbench</pre><p class="muted small">When enabled, the provider receives the objective, constraints, prior agent advice, scientific result summaries, and attachment metadata. Confirm the data can be shared. The call limit caps requests, not cost; timed-out requests may still be billed.</p></section></div><section class="panel"><h2>Current Implementation</h2><p class="result-text">Connected: 10 scientific adapters for structure, molecular, retrosynthesis, and solid-state analyses, with result and evidence tracking.\nStill needed: a licensed and validated cocrystal model, ADMET, pharmacology evidence for drug pairs, validated experimental SOPs, real experimental data, cost controls, and cloud deployment.\nWorkflow completion means the stages finished; it does not establish drug efficacy, clinical synergy, or formation of a new cocrystal phase.</p></section>`;
}

function detail(run) {
  state.detail = run;
  const stages = run.pack.stages;
  const stage = stages.find(s=>s.id===state.stage) || stages[Math.min(run.step, stages.length-1)];
  const output = run.outputs[stage.id];
  const active = ['draft','queued','running','awaiting_review'].includes(run.status);
  app.innerHTML = `<button class="back" data-back>← Back to Research Workspace</button>` +
    `<div class="page-heading detail-heading"><div><div class="eyebrow">${escape(run.pack.name)} / ${run.id.slice(0,8)}</div><h1>${escape(run.title)}</h1><p>${escape(projectName(run.project_id))} · ${date(run.created_at)} · ${run.use_model?'LLM advice + local scientific adapters':'rule-based orchestration + local scientific adapters'}</p></div>${statusChip(run.status)}</div>` +
    `<div class="detail-grid"><div><section class="panel"><h3 class="panel-title">Research workflow <span class="muted">${run.step}/${stages.length}</span></h3>${stages.map((s,i)=>`<button class="stage-button ${stage.id===s.id?'selected':''} ${run.outputs[s.id]?'done':''} ${run.step===i?'current':''}" data-stage="${s.id}"><span class="stage-number">${run.outputs[s.id]?'✓':i+1}</span><span>${escape(s.name)}<small>${escape(s.role)}</small></span></button>`).join('')}</section><section class="panel"><h3 class="panel-title">Run actions</h3><div class="actions">${run.status==='draft'?'<button class="button primary full" data-action="start">Start workflow →</button>':''}${active?'<button class="button danger full" data-action="cancel">Cancel run</button>':''}<a class="button full" href="/api/runs/${run.id}/report">↓ Export research report</a><a class="button full" href="/api/runs/${run.id}/export">↓ Export full JSON</a><button class="button full" data-next>Create next iteration</button></div><p class="form-hint">LLM requests ${run.model_attempts}/${run.max_model_calls} requests · successful ${run.model_calls} requests</p></section></div><div>` +
    (run.status==='awaiting_review'?`<section class="panel review-box"><h3>Researcher review required</h3><p>Review the completed calculations, evidence boundaries, and open requirements. Approval compiles the existing results; it does not trigger wet-lab work. Cancel and create another iteration to change the objective.</p><textarea id="review-note" rows="2" maxlength="6000" aria-label="Review notes" placeholder="Add review notes, constraints for the next iteration, or questions to validate">${escape(state.reviewNote)}</textarea><button class="button primary" data-action="approve">Approve and generate report →</button></section>`:'') +
    `<section class="panel"><h3 class="panel-title">${escape(stage.name)} ${outputBadge(output,stage)}</h3><div class="fact-list">${chip(stage.role)}${stage.tool?chip(stage.tool):''}</div><div class="result-text">${escape(output?output.summary:'This stage has not run yet. Its outputs and open requirements will appear here.')}</div>${output?.advice?`<div class="instruction"><h3>LLM Advice</h3><p>LLM-generated research advice; not scientifically validated.</p><div class="result-text">${escape(output.advice)}</div></div>`:''}${output?.model_error?`<p class="error-text">${escape(output.model_error)}</p>`:''}${stage.instruction?`<div class="instruction"><h3>Stage requirements</h3><p>${escape(stage.instruction)}</p></div>`:''}${output?.attachments?.length?`<div class="instruction"><h3>Attachment Integrity</h3>${output.attachments.map(f=>`<p>${escape(f.name)} · ${f.integrity?'Hash verified':'Integrity check failed'} · Not analyzed at upload; parsed by scientific stages</p>`).join('')}</div>`:''}</section>` +
    `<section class="panel"><h3>Research objective</h3><div class="result-text">${escape(run.objective)}</div><div class="instruction"><h3>Constraints & Existing Evidence</h3><div class="result-text">${escape(run.constraints||'Not provided')}</div></div></section>` +
    `<section class="panel"><h3 class="panel-title">Research files ${run.status==='draft'?'<label class="button upload-label">＋ Add file<input id="file-input" type="file" accept=".txt,.md,.csv,.tsv,.json,.pdb,.cif,.sdf,.mol,.smi,.pdf"></label>':''}</h3><p class="muted small">Files are limited to 5 MB. Structures, text, tables, and PDFs are accepted. Structure and numeric data are parsed by scientific stages; PDF contents are not extracted.</p>${run.files.length?run.files.map(f=>`<div class="file-item"><span>${escape(f.name)}<small>SHA-256 ${f.sha256.slice(0,24)}…</small></span><span class="muted small">${Math.ceil(f.size/1024)} KB</span></div>`).join(''):'<p class="muted small">No files added. Add inputs to the draft before starting the run.</p>'}</section>` +
    `<section class="panel"><h3>Activity Log</h3>${run.events.slice().reverse().map(e=>`<div class="event"><time>${date(e.created_at)}</time><span>${escape(e.message)}</span></div>`).join('')}</section></div></div>`;
  const resultPanel=app.querySelector('.detail-grid>div:nth-child(2) .panel:not(.review-box)');
  if(resultPanel)resultPanel.insertAdjacentHTML('beforeend',scienceMarkup(output,run));
  if(resultPanel&&output?.mode==='scientific'){
    const badge=resultPanel.querySelector('.panel-title .chip');
    if(badge)badge.textContent={computed:'Computed',partial:'Partial result',needs_input:'Missing input',needs_dependency:'Missing dependency',invalid_input:'Invalid input',failed:'Computation failed',timeout:'Computation timed out'}[output.status]||output.status;
  }
  applyPermissions();
}

function outputBadge(output,stage){
  if(!output)return chip(stage.kind==='approval'?'Human review':'Not run yet',stage.kind==='approval'?'amber':'');
  if(output.mode==='model_advisory')return chip('LLM advice','blue');
  if(output.status==='reviewed')return chip('Reviewed','amber');
  const label={computed:'Computed',partial:'Partial computation / open requirements',needs_input:'Waiting for scientific inputs',needs_dependency:'Missing dependency',invalid_input:'Input needs correction',failed:'Computation failed',timeout:'Computation timed out',planning:'Orchestrated',unavailable:'Engine not connected'}[output.status]||output.status;
  const tone=output.status==='computed'?'green':['partial','needs_input','needs_dependency'].includes(output.status)?'amber':['failed','timeout','invalid_input','unavailable'].includes(output.status)?'red':'blue';
  return chip(label,tone);
}

function applyPermissions(){
  document.querySelectorAll('[data-create],[data-next],[data-action=start],[data-action=cancel],.upload-label').forEach(el=>{el.hidden=!can('write');});
  document.querySelectorAll('[data-action=approve],#review-note').forEach(el=>{el.hidden=!can('review');});
}

function authView(register=false){
  document.body.classList.add('auth-mode');
  const identity=register?'<label>Email address<input name="email" type="email" required maxlength="254" autocomplete="email" placeholder="name@example.com"></label>':'<label>Email or existing username<input name="identifier" required maxlength="254" autocomplete="username" placeholder="Email address or existing username"></label>';
  app.innerHTML=`<div class="auth-shell"><div class="auth-brand"><span>a·</span>research atlas</div><section class="auth-card"><div class="eyebrow">YOUR RESEARCH SPACE</div><h1>${register?'Create an account with email':'Welcome to Research Atlas'}</h1><p class="muted">${register?(state.boot?.setup_required?'Your first account inherits existing local projects, runs, and research files.':'Each new account gets an independent workspace or can join a team with an invitation code.'):'Sign in to access your projects, research records, and team workspaces.'}</p><form id="auth-form" data-mode="${register?'register':'login'}">${register?'<label>Display name<input name="display_name" required maxlength="80" autocomplete="nickname" placeholder="Your name or research alias"></label>':''}${identity}<label>Password<input name="password" type="password" required ${register?'minlength="12"':''} maxlength="128" autocomplete="${register?'new-password':'current-password'}" placeholder="${register?'At least 12 characters':'Enter your password'}"></label>${register?'<label>Confirm password<input name="confirm_password" type="password" required minlength="12" maxlength="128" autocomplete="new-password" placeholder="Enter your password again"></label><p class="form-hint">After registering, request an email verification message from My Account.</p>':''}<p class="error-text" id="auth-error" role="alert"></p><button class="button primary full" type="submit">${register?'Create account and continue':'Sign in'} →</button></form><button class="text-button auth-switch" data-auth-mode="${register?'login':'register'}">${register?'Already have an account? Sign in':'New here? Create an account'}</button></section><p class="auth-foot">Email and password sign-in · Existing accounts supported · Workspace-isolated data</p></div>`;
  if(!register)$('#auth-form').insertAdjacentHTML('afterend','<button class="text-button auth-switch" data-forgot>Forgot password? Reset by email</button>');
  if(register&&state.boot?.mail?.configured){$('.form-hint',$('#auth-form')).textContent='After registering, request and confirm an email verification message in My Account.';}
}

function emailLink(){const match=location.hash.match(/^#(verify-email|reset-password)\/([A-Za-z0-9_-]{40,100})$/);return match?{kind:match[1],token:match[2]}:null;}
function mailShell(title,body){document.body.classList.add('auth-mode');app.innerHTML=`<div class="auth-shell"><div class="auth-brand"><span>a·</span>research atlas</div><section class="auth-card"><h1>${title}</h1>${body}<button class="text-button auth-switch" data-return-account>Back to${state.boot?.user?'My Account':'sign-in page'}</button></section></div>`;}
function forgotView(){
  const ready=state.boot?.mail?.configured;
  mailShell('Reset Password',`<p class="muted">Enter your registered email. The reset link expires in 30 minutes and can only be used once.</p><form id="forgot-form"><label>Email address<input name="email" type="email" required maxlength="254" autocomplete="email" placeholder="name@example.com"></label><p class="error-text" role="alert"></p><p class="form-success" role="status"></p><button class="button primary full" type="submit" ${ready?'':'disabled'}>Send password reset email</button></form>${ready?'':'<p class="muted">The administrator has not configured Gmail / SMTP email delivery; password reset is unavailable.</p>'}`);
}
function emailActionView(action){
  if(action.kind==='verify-email'){
    mailShell('Confirm Email Verification',state.boot?.user?`<p class="muted">Current account: ${escape(state.boot.user.email||state.boot.user.username)} Confirm this is the email address that received the message, then enter your current password.</p><form id="verify-form"><label>Current password<input name="current_password" type="password" required maxlength="128" autocomplete="current-password"></label><p class="error-text" role="alert"></p><p class="form-success" role="status"></p><button class="button primary full" type="submit">Verify email</button></form>`:'<p class="muted">Sign in to the account that received the verification email. Opening the link will not sign you in or verify the address automatically.</p><button class="button primary full" data-email-login>Sign in to continue verification</button>');
  }else{
    mailShell('Set a New Password','<p class="muted">Resetting your password revokes all existing sessions. Sign in again with the new password.</p><form id="reset-form"><label>New password<input name="new_password" type="password" required minlength="12" maxlength="128" autocomplete="new-password"></label><label>Confirm new password<input name="confirm_password" type="password" required minlength="12" maxlength="128" autocomplete="new-password"></label><p class="error-text" role="alert"></p><p class="form-success" role="status"></p><button class="button primary full" type="submit">Reset password</button></form>');
  }
}

function account(){
  const user=state.boot.user;
  app.innerHTML=heading('YOUR ACCOUNT','My Account','Manage your sign-in identity and account security.')+`<div class="config-grid"><section class="panel"><h2>${escape(user.display_name)}</h2><p class="account-meta">${user.email?'Email: '+escape(user.email)+' '+chip(user.email_verified?'Verified':'Unverified',user.email_verified?'green':'amber'):'Username: '+escape(user.username)}<br>Current workspace: ${escape(state.boot.workspace.name)}<br>Workspace role: ${roleNames[state.boot.workspace.role]}</p><button class="button" data-logout>Sign out</button><p class="form-hint">Sessions last up to 7 days. Signing out ends the current session immediately.</p></section><section class="panel"><h2>${user.email?'Change sign-in email':'Add sign-in email'}</h2><form id="email-form"><label>Email address<input name="email" type="email" required maxlength="254" autocomplete="email" value="${escape(user.email||'')}" placeholder="name@example.com"></label><label>Current password<input type="password" name="current_password" required maxlength="128" autocomplete="current-password"></label><p class="error-text" role="alert"></p><button class="button primary" type="submit">Save email and sign in again</button></form><p class="form-hint">Your current password is required. The email must be verified before password recovery is available. Saving revokes all sessions.</p></section><section class="panel"><h2>Change password</h2><form id="password-form"><label>Current password<input type="password" name="current_password" required maxlength="128" autocomplete="current-password"></label><label>New password<input type="password" name="new_password" required minlength="12" maxlength="128" autocomplete="new-password"></label><label>Confirm new password<input type="password" name="confirm_password" required minlength="12" maxlength="128" autocomplete="new-password"></label><p class="error-text" role="alert"></p><button class="button primary" type="submit">Update password and sign in again</button></form><p class="form-hint">Changing your password revokes all active sessions.</p></section></div>`;
  const panel=document.createElement('section');panel.className='panel';
  document.querySelectorAll('.form-hint').forEach(el=>{if(el.textContent.includes('Email must be verified'))el.textContent=state.boot.mail?.configured?'Saving revokes all sessions; verify the new email address.':'Email delivery is not configured; saving will revoke all sessions.';});
  const last=state.boot.mail?.last;
  panel.innerHTML=`<h2>Email verification</h2><p class="muted small">${user.email_verified?'Your email is verified.':user.email?'Verification link sent to '+escape(user.email)+'; it expires in 60 minutes.':'Add an email address before requesting verification.'}</p>${user.email&&!user.email_verified?`<button class="button primary" data-request-verification ${state.boot.mail?.configured?'':'disabled'}>Send / Resend Verification Email</button>`:''}<p class="form-success" id="verification-status"></p>${!state.boot.mail?.configured?'<p class="form-hint">Gmail / SMTP email delivery is not configured.</p>':''}${last?`<p class="form-hint">Latest email: ${last.kind==='verify'?'email verification':'password reset'} · ${{queued:'Queued',sending:'Sending',sent:'Accepted by the mail server; check your inbox',failed:'Send failed. Check SMTP settings and try again',cancelled:'Cancelled'}[last.status]||escape(last.status)}</p>`:''}`;
  app.children[0].after(panel);
}

async function team(){
  const members=await api('/api/members');
  if(location.hash!=='#team')return;
  const admin=can('members');
  app.innerHTML=heading('WORKSPACE & MEMBERS','Team & Access',escape(state.boot.workspace.name))+`<div class="role-banner">Current identity: ${roleNames[state.boot.workspace.role]}. Members share all projects in a workspace. Separate workspaces are isolated.</div><section class="panel"><h2>Workspace members</h2>${members.map(m=>`<div class="member-row"><div class="member-info"><strong>${escape(m.display_name)}${m.id===state.boot.user.id?' (you)':''}</strong><small>@${escape(m.username)}</small></div>${admin?`<select aria-label="${escape(m.username)} role" id="role-${m.id}">${Object.entries(roleNames).map(([r,n])=>`<option value="${r}" ${r===m.role?'selected':''}>${n}</option>`).join('')}</select><button class="button" data-save-member="${m.id}">Save</button><button class="button danger" data-remove-member="${m.id}">Remove</button>`:chip(roleNames[m.role])}</div>`).join('')}</section>`+
    (admin?`<section class="panel"><h2>Invite Members</h2><p class="muted small">Create a single-use invitation code and share it with a teammate. It expires in 7 days and can only be used once. Code holders receive the selected role, so share it carefully.</p><form id="invite-form"><label>Role to assign<select name="role">${['researcher','reviewer','viewer','admin'].map(r=>`<option value="${r}">${roleNames[r]}</option>`).join('')}</select></label><p class="error-text"></p><button class="button primary" type="submit">Generate invitation code</button></form><div id="invite-result"></div></section>`:'')+
    `<div class="team-forms"><section class="panel"><h2>Join a Workspace</h2><form id="join-form"><label>Invitation code<input name="code" required maxlength="100" autocomplete="off" placeholder="Ask your workspace admin for an invitation code"></label><p class="error-text"></p><button class="button" type="submit">Join workspace</button></form></section><section class="panel"><h2>Create a Workspace</h2><p class="muted small">Create separate workspaces for distinct teams or isolated collaborations.</p><form id="workspace-form"><label>Workspace name<input name="name" required maxlength="100" placeholder="e.g. Cocrystal Research Team"></label><p class="error-text"></p><button class="button" type="submit">Create workspace</button></form></section></div>`;
  if(admin){
    const events=await api('/api/audit');
    if(location.hash!=='#team')return;
    const panel=document.createElement('section');panel.className='panel';
    panel.innerHTML='<h2>Recent Access and Activity</h2><div class="audit-table">'+events.map(e=>`<div class="event"><time>${date(e.created_at)}</time><span>${escape(e.display_name||'Member')} · ${escape(e.action)}</span></div>`).join('')+'</div>';
    app.append(panel);
  }
}

function announceAuth(){localStorage.setItem('atlas-auth-event',String(Date.now())+Math.random());}
async function reloadWorkspace(workspace){if(workspace)localStorage.setItem('atlas-workspace',workspace);location.hash='workspace';location.reload();}
$('#workspace-select').addEventListener('change',event=>reloadWorkspace(event.target.value));
window.addEventListener('storage',event=>{if(event.key==='atlas-auth-event')location.reload();});
document.addEventListener('click',async event=>{
  const b=event.target.closest('button');if(!b)return;
  if(b.hasAttribute('data-forgot')){location.hash='forgot-password';forgotView();return;}
  if(b.hasAttribute('data-email-login')){authView(false);return;}
  if(b.hasAttribute('data-return-account')){location.hash=state.boot?.user?'account':'workspace';initialize();return;}
  if(b.hasAttribute('data-request-verification')){b.disabled=true;try{const result=await api('/api/auth/request-verification',{});$('#verification-status').textContent=result.message;}catch(e){toast(e.message);}finally{b.disabled=false;}return;}
  if(b.dataset.authMode){authView(b.dataset.authMode==='register');return;}
  if(b.hasAttribute('data-logout')){try{await api('/api/auth/logout',{});announceAuth();location.reload();}catch(e){toast(e.message);}return;}
  const target=b.dataset.saveMember||b.dataset.removeMember;
  if(target){
    b.disabled=true;
    try{await api('/api/members/'+target,b.dataset.saveMember?{role:$('#role-'+target).value}:{action:'remove'});if(target===state.boot.user.id)location.reload();else{await initialize();toast('Member access updated');}}
    catch(e){toast(e.message);b.disabled=false;}
  }
});
document.addEventListener('submit',async event=>{
  const form=event.target;
  if(!['auth-form','password-form','email-form','invite-form','join-form','workspace-form','forgot-form','verify-form','reset-form'].includes(form.id))return;
  event.preventDefault();const button=$('button[type=submit]',form);button.disabled=true;const error=$('.error-text',form);error.textContent='';
  const data=Object.fromEntries(new FormData(form).entries());
  try{
    if(form.id==='forgot-form'){
      const result=await api('/api/auth/forgot-password',data);$('.form-success',form).textContent=result.message;
    }else if(form.id==='verify-form'){
      const action=emailLink();if(!action)throw new Error('Invalid verification link');
      await api('/api/auth/verify-email',{token:action.token,current_password:data.current_password});location.hash='account';await initialize();toast('Email verified');
    }else if(form.id==='reset-form'){
      if(data.new_password!==data.confirm_password)throw new Error('The passwords do not match');
      const action=emailLink();if(!action)throw new Error('Invalid reset link');
      await api('/api/auth/reset-password',{token:action.token,new_password:data.new_password});announceAuth();location.hash='workspace';await initialize();toast('Password reset. Sign in with your new password.');
    }else if(form.id==='auth-form'){
      if(form.dataset.mode==='register'&&data.password!==data.confirm_password)throw new Error('The passwords do not match');
      await api('/api/auth/'+form.dataset.mode,data);announceAuth();state.runs=[];state.detail=null;state.project='all';if(!emailLink())location.hash='workspace';await initialize();
    }else if(form.id==='password-form'){
      if(data.new_password!==data.confirm_password)throw new Error('The passwords do not match');
      await api('/api/auth/password',data);announceAuth();location.reload();
    }else if(form.id==='email-form'){
      await api('/api/auth/email',data);announceAuth();location.reload();
    }else if(form.id==='invite-form'){
      const result=await api('/api/invitations',data);
      $('#invite-result').innerHTML=`<div class="invite-result"><div class="invite-code">${escape(result.code)}</div><p>${roleNames[result.role]} access · Expires ${date(result.expires_at*1000)} · Copy and share this code securely.</p></div>`;
    }else if(form.id==='join-form'){
      const result=await api('/api/invitations/accept',data);await reloadWorkspace(result.workspace_id);
    }else{
      const result=await api('/api/workspaces',data);await reloadWorkspace(result.workspace_id);
    }
  }catch(e){error.textContent=e.message;}finally{button.disabled=false;}
});

async function route() {
  const action=emailLink();if(action){emailActionView(action);return;}
  if(location.hash==='#forgot-password'){forgotView();return;}
  if(!state.boot?.user)return;
  document.body.classList.remove('auth-mode');
  const path = location.hash.slice(1) || 'workspace';
  const view = path.startsWith('run/') ? 'workspace' : path;
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
  $('#breadcrumb').innerHTML = `Workspace <span>/</span> ${{workspace:'Research Workspace',domains:'Domains & Capabilities',settings:'Runtime Settings',team:'Team & Access',account:'My Account'}[view]||'Research runs'}`;
  try {
    if (path.startsWith('run/')) {
      const run = await api('/api/runs/'+encodeURIComponent(path.slice(4)));
      if (location.hash.slice(1) !== path) return;
      state.signature = JSON.stringify(run); detail(run);
    } else if (path==='domains') domains();
    else if (path==='settings') settings();
    else if (path==='team') await team();
    else if (path==='account') account();
    else {await refreshRuns();if((location.hash.slice(1)||'workspace')===path)workspace();}
  } catch(e) { if(!state.boot?.user)return;app.innerHTML=`<section class="panel"><h2>Unable to open this run</h2><p class="error-text">${escape(e.message)}</p><button class="button" data-back>Back to workspace</button></section>`; }
}

function openCreate(packId='', previous=null) {
  if(!can('write')){toast('Your role does not have permission to create research runs');return;}
  const form = $('#create-form'); form.reset(); $('#form-error').textContent='';
  $('#pack-select').innerHTML = state.boot.packs.map(p=>`<option value="${p.id}">${escape(p.name)}</option>`).join('');
  if (packId) $('#pack-select').value = packId;
  if (state.project!=='all') $('#project-select').value=state.project;
  $('#use-model').disabled=!state.boot.model.configured;
  $('#model-hint').textContent=state.boot.model.configured?'LLM advice uses an external service. Confirm that your inputs may be shared. The limit counts request attempts.':'No LLM provider is configured. Rule-based orchestration and local scientific adapters remain available.';
  updatePack();
  if (previous) {
    $('#science-input').value=JSON.stringify(previous.science||{},null,2);
    form.elements.title.value = previous.title.slice(0,110)+' · next iteration';
    form.elements.objective.value = previous.objective;
    form.elements.constraints.value = (previous.constraints+'\nPrevious run: '+previous.id+'\nPrevious review: '+(state.reviewNote || previous.review?.note || 'Not provided')+'\nChanges for this iteration: ').slice(0,6000);
    form.elements.project_id.value = previous.project_id;
    $('#model-hint').textContent += ' Attachments are not copied to the next iteration. Add the required files again.';
  }
  $('#create-dialog').showModal();
}
function updatePack() { const p=state.boot.packs.find(p=>p.id===$('#pack-select').value); $('#objective-label').textContent=p.input_label; $('#objective').placeholder=p.placeholder; }

document.addEventListener('click', async event=>{
  const b=event.target.closest('button'); if(!b) return;
  if(b.hasAttribute('data-fill-science')){try{$('#science-input').value=JSON.stringify(await api('/api/science/example/'+$('#pack-select').value),null,2);}catch(e){toast(e.message);}return;}
  if(b.dataset.view) {location.hash=b.dataset.view; return;}
  if(b.hasAttribute('data-create')) {openCreate(b.dataset.create);return;}
  if(b.hasAttribute('data-project')) {state.project=b.dataset.project;sidebar();if(location.hash==='#workspace'||!location.hash) workspace();else location.hash='workspace';return;}
  if(b.dataset.filter) {state.filter=b.dataset.filter;workspace();return;}
  if(b.hasAttribute('data-back')) {location.hash='workspace';return;}
  if(b.dataset.stage) {state.stage=b.dataset.stage;detail(state.detail);return;}
  if(b.hasAttribute('data-next')) {openCreate(state.detail.pack.id,state.detail);return;}
  if(b.dataset.action) {
    b.disabled=true;
    try {const run=await api(`/api/runs/${state.detail.id}/${b.dataset.action}`,{note:state.reviewNote});state.stage=null;state.signature=JSON.stringify(run);detail(run);await refreshRuns();toast(b.dataset.action==='approve'?'Review saved':b.dataset.action==='start'?'Workflow queued':'Run cancelled');}
    catch(e){toast(e.message);b.disabled=false;}
  }
});
document.addEventListener('click', event=>{const row=event.target.closest('[data-run]');if(row){state.stage=null;state.reviewNote='';location.hash='run/'+row.dataset.run;}});
document.addEventListener('keydown', event=>{if(event.key==='Enter'&&event.target.matches('[data-run]'))event.target.click();});
document.addEventListener('input', event=>{if(event.target.id==='review-note')state.reviewNote=event.target.value;});
document.addEventListener('change', async event=>{
  if(event.target.id!=='file-input')return;
  const file=event.target.files[0];if(!file)return;
  if(file.size>5*1024*1024){toast('Files must be no larger than 5 MB');event.target.value='';return;}
  event.target.disabled=true;
  try {
    const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
    const run=await api(`/api/runs/${state.detail.id}/files`,{name:file.name,content});state.signature=JSON.stringify(run);detail(run);toast('File saved. Scientific stages will parse it when the workflow starts.');
  }catch(e){toast(e.message||'File upload failed');if(event.target.isConnected)event.target.disabled=false;}
});
$('#pack-select').addEventListener('change',updatePack);
$('#close-dialog').addEventListener('click',()=>$('#create-dialog').close());
$('#close-project').addEventListener('click',()=>$('#project-dialog').close());
$('#add-project').addEventListener('click',()=>{$('#project-form').reset();$('#project-error').textContent='';$('#project-dialog').showModal();});
$('#project-form').addEventListener('submit',async event=>{event.preventDefault();const button=$('button[type="submit"]',event.target);button.disabled=true;try{const p=await api('/api/projects',{name:event.target.elements.name.value});state.boot.projects.push(p);state.project=p.id;sidebar();$('#project-dialog').close();location.hash='workspace';workspace();toast('Project created');}catch(e){$('#project-error').textContent=e.message;}finally{button.disabled=false;}});
$('#create-form').addEventListener('submit',async event=>{
  event.preventDefault();const form=event.target;const button=$('button[type="submit"]',form);button.disabled=true;
  try {
    const fields=new FormData(form);const data=Object.fromEntries(fields.entries());data.use_model=$('#use-model').checked;data.max_model_calls=Number(data.max_model_calls);
    try{data.science=data.science_json.trim()?JSON.parse(data.science_json):{};}catch(e){throw new Error('Invalid scientific-input JSON. Check the quotes, commas, and brackets.');}delete data.science_json;
    const run=await api('/api/runs',data);$('#create-dialog').close();state.stage=null;state.reviewNote='';await refreshRuns();location.hash='run/'+run.id;
  }catch(e){$('#form-error').textContent=e.message;}finally{button.disabled=false;}
});
async function refreshRuns(){state.runs=await api('/api/runs');}
async function poll(){
  if(state.polling||!state.boot?.user||document.hidden)return;state.polling=true;
  try {
    const path=location.hash.slice(1);
    if(path.startsWith('run/')) {
      const run=await api('/api/runs/'+encodeURIComponent(path.slice(4)));const signature=JSON.stringify(run);
      if(location.hash.slice(1)===path&&signature!==state.signature){state.signature=signature;detail(run);}
    }else if(!path||path==='workspace'){
      const previous=JSON.stringify(state.runs);await refreshRuns();if(previous!==JSON.stringify(state.runs))workspace();
    }
    $('#connection').textContent='Connected to local service';
  }catch(e){$('#connection').textContent='Connection lost. Check the local service.';}finally{state.polling=false;}
}
window.addEventListener('hashchange',()=>{state.stage=null;state.reviewNote='';route();});
async function initialize(){
  app.innerHTML='<div class="loading">Loading Research Atlas…</div>';
  try{state.boot=await api('/api/bootstrap');if(!state.boot.user){if(emailLink()||location.hash==='#forgot-password')await route();else authView(state.boot.setup_required);return;}document.body.classList.remove('auth-mode');localStorage.setItem('atlas-workspace',state.boot.workspace.id);await refreshRuns();sidebar();$('#connection').textContent='Connected to local service';await route();}
  catch(e){app.innerHTML=`<section class="panel"><h2>Local service is not ready</h2><p>${escape(e.message)}</p><p class="muted">Run python3 -m workbench, then refresh this page.</p></section>`;}
}
initialize();
setInterval(poll,1500);
