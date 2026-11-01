const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const stamp = s => s ? new Date(s).toLocaleString('zh-CN', {hour12:false}) : '—';
const labels = {QUEUED:'排队中',RUNNING:'执行中',SUCCEEDED:'成功',FAILED:'失败',CANCELLED:'已取消',CANCELLING:'取消中',PENDING:'等待依赖',READY:'等待执行',RETRY_WAIT:'退避重试',CACHED:'已复用',LOST:'失联',TIMED_OUT:'超时',BLOCKED:'被阻塞',AVAILABLE:'空闲',BUSY:'占用中',QUARANTINED:'隔离中'};
const badge = status => `<span class="badge ${esc(status)}">${esc(labels[status] || status)}</span>`;
let selectedStep = null, snapshot = null, taskCursor = null, browsingOlder = false;
let eventRows = [], eventNewest = 0, eventOldest = null, eventInitialized = false, eventLoading = false, timingRows = [];
function toast(message) { $('#toast').textContent=message; $('#toast').classList.add('show'); setTimeout(()=>$('#toast').classList.remove('show'),6500); }
async function api(path, options={}) { const res=await fetch(path,options); let data; try {data=await res.json();} catch {throw new Error(`HTTP ${res.status}`);} if(!res.ok) throw new Error(data.message || JSON.stringify(data.detail) || data.code); return data; }
function updated() {if($('#updated')) $('#updated').textContent=`更新于 ${new Date().toLocaleTimeString()} · 每 2 秒刷新`;}
async function renderResources() {
 const d=await api('/api/resources');
 $('#resource-list').innerHTML=d.slots.map(r=>`<div class="resource" data-slot="${esc(r.id)}"><div class="resource-title"><b>${esc(r.key)}</b>${badge(r.status)}</div><p>${esc(r.node_id)} · 模拟 ${r.kind.toUpperCase()} · ${r.sample_fresh?'数据新鲜':'数据过期 / 不可用'}</p><div class="meter"><span style="width:${r.sample_fresh?r.sample.utilization_percent:0}%"></span></div><p>${r.sample_fresh?`模拟利用率 ${r.sample.utilization_percent}% · 显存 ${r.sample.memory_used_mb}/${r.sample.memory_total_mb} MB`:'最近采样：'+stamp(r.measured_at)}</p><p>${r.holder?`占用任务 <a href="/tasks/${esc(r.holder.task_id)}">${esc(r.holder.task_id.slice(0,8))} ↗</a>`:'无当前持有者'}${r.reason?` · ${esc(r.reason)}`:''}</p></div>`).join('') || '<p class="muted">尚无资源注册，请启动 Worker。</p>';
 await renderHistory(d);
 $('#workers').textContent=`Worker 在线 ${d.workers.filter(w=>w.online).length} · 等待执行 / 重试 ${d.waiting.length} 步 · 遥测来源 MOCK，不代表真实 GPU。`;
}
function taskRows(items) { return items.map(t=>`<tr><td><a href="/tasks/${esc(t.id)}">${esc(t.request.name)} ↗</a><small>${esc(t.id.slice(0,8))} · ${esc(t.request.asset_id)}</small></td><td>${badge(t.status)}</td><td>第 ${t.epoch} 次</td><td>${stamp(t.created_at)}</td><td>${t.stop_reason?`<span class="error">${esc(t.stop_reason)}</span>`:`<a href="/tasks/${esc(t.id)}">查看执行 →</a>`}</td></tr>`).join(''); }
async function renderList(append=false) {
 const status=$('#status-filter').value;
 const d=await api('/api/tasks?limit=30'+(status?'&status='+status:'')+(append&&taskCursor?'&cursor='+taskCursor:''));
 if(append) {browsingOlder=true;}
 if(append) $('#task-list').insertAdjacentHTML('beforeend',taskRows(d.items));
 else $('#task-list').innerHTML=taskRows(d.items)||'<tr><td colspan="5" class="empty">还没有任务。从上方创建第一个处理任务。</td></tr>';
 taskCursor=d.next_cursor; $('#more-tasks').hidden=!taskCursor;
 const o=await api('/api/overview');
 $('#stats').innerHTML=[['执行中',(o.tasks.RUNNING||0)+(o.tasks.QUEUED||0)],['已完成',o.tasks.SUCCEEDED||0],['失败任务',o.tasks.FAILED||0],['复用步骤',o.cached_steps]].map(([name,n])=>`<div class="stat">${name}<strong>${n}</strong></div>`).join('');
}
function renderStep() {
 const x=snapshot.steps.find(s=>s.key===selectedStep)||snapshot.steps[0]; selectedStep=x.key;
 $('#step-title').textContent=x.spec.name+' · 执行尝试'; $('#step-resource').textContent=x.spec.resource.toUpperCase();
 $('#step-info').innerHTML=`${badge(x.status)} <span class="muted">${esc(x.key)} · 当前进度 ${Math.round(x.progress)}%</span><p class="muted">依赖：${esc(x.spec.deps.join('、')||'素材输入')}<br>等待原因：${esc(x.wait_reason||'—')}<br>下次重试：${stamp(x.next_run_at)}<br>最近进度：${stamp(x.last_progress_at)}</p>${x.error?`<p class="error">${esc(x.error)}</p>`:''}`;
 renderCheckpointSummary(x);
 $('#attempts').innerHTML=x.attempts.map(a=>`<article class="attempt"><header><b>Attempt ${a.number} · 第 ${a.epoch} 代次</b>${badge(a.status)}</header><p>${stamp(a.started_at||a.claimed_at)} → ${stamp(a.finished_at)}</p><p>阶段 ${esc(a.phase)} · Fence ${a.fence} · 清理 ${esc(a.cleanup_state)}</p><p>排队 ${seconds(timingRows.find(t=>t.attempt_id===a.id)?.queue_seconds)} · 运行 ${seconds(timingRows.find(t=>t.attempt_id===a.id)?.run_seconds)}</p><p>最近心跳 ${stamp(a.heartbeat_at)}<br>租约截止 ${stamp(a.lease_expires_at)}</p><p>Worker ${esc(a.worker_id.slice(0,8))} · ${esc(a.slot_id)}</p>${a.error_code?`<p class="error">${esc(a.error_code)} · ${a.retryable?'允许策略重试':'终止或控制操作'}</p>`:''}</article>`).join('')||`<p class="muted">${x.status==='CACHED'?'已复用有效产物，本次没有创建计算尝试。':'尚未开始计算。等待资源不会消耗尝试次数。'}</p>`;
 document.querySelectorAll('.node').forEach(n=>n.classList.toggle('selected',n.dataset.key===selectedStep));
}
async function renderDetail() {
 const id=$('#task-view').dataset.taskId; snapshot=await api('/api/tasks/'+id); const t=snapshot;
 timingRows=(await api('/api/tasks/'+id+'/timings')).items;
 initializeEventFilters(t);
 $('#task-title').textContent=t.request.name;
 $('#task-meta').textContent=`${t.id} · ${t.request.asset_id} · 第 ${t.epoch} 代次 · 创建于 ${stamp(t.created_at)}`;
 $('#task-summary').innerHTML=`<div>${badge(t.status)} &nbsp; <strong>已完成 ${t.completed_steps} / ${t.steps.length} 步</strong></div><span class="muted">${t.stop_reason?esc(t.stop_reason):'等待 / 进度请查看步骤节点'} · 截止 ${stamp(t.deadline_at)}</span>`;
 $('#resume').hidden=t.status!=='FAILED'; $('#cancel').hidden=!['RUNNING','QUEUED'].includes(t.status);
 $('#dag').innerHTML=t.steps.map((s,i)=>`<button class="node" data-key="${esc(s.key)}"><span class="num">STEP ${String(i+1).padStart(2,'0')} · ${s.spec.resource.toUpperCase()}</span><b>${esc(s.spec.name)}</b>${badge(s.status)}<div class="meter"><span style="width:${s.progress}%"></span></div><small>← ${esc(s.spec.deps.join(' + ')||'输入素材')}</small></button>`).join('');
 document.querySelectorAll('.node').forEach(n=>n.addEventListener('click',()=>{selectedStep=n.dataset.key;renderStep();})); renderStep();
 await syncEvents();
 $('#artifact-list').innerHTML=t.steps.filter(s=>s.artifact).map(s=>{const a=s.artifact;return `<div class="artifact"><b>${esc(s.spec.name)}</b> &nbsp; ${badge(s.status)}<p>${a.available?'✓ 文件完整性通过':'⚠ 产物不可用'} · MOCK<br>来源 Attempt ${esc(a.producer_attempt_id)}<br>SHA256 ${esc(a.content_digest.slice(0,20))}…</p>${a.available?[...a.manifest.files.map(f=>f.key),'manifest.json'].map(k=>`<a href="/api/artifacts/${esc(a.id)}/files/${esc(k)}">↓ ${esc(k)}</a>`).join(''):''}</div>`;}).join('')||'<p class="muted">尚无正式产物，临时文件不会展示为结果。</p>';
}
async function control(action) {const button=$('#'+action);button.disabled=true;try {await api(`/api/tasks/${snapshot.id}/${action}`,{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({expected_revision:snapshot.revision})}); await renderDetail();toast(action==='resume'?'已恢复，有效成功步骤保留':'取消请求已提交');}catch(e){toast(e.message);}finally{button.disabled=false;}}
async function refresh() {try {if($('#task-view'))await renderDetail();else if(!browsingOlder) await renderList();await renderResources();updated();}catch(e){if($('#updated'))$('#updated').textContent='连接中断，展示的是旧数据';toast(e.message);}finally{setTimeout(refresh,document.hidden?10000:2000);}}
if($('#create-form')){
 $('#create-form').addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(e.target),button=e.target.querySelector('button');button.disabled=true;try {const scenario=f.get('scenario');const data={name:f.get('name'),asset_id:f.get('asset_id'),scenario,execution_profile:f.get('execution_profile'),parameters:{seed:Number(f.get('seed')),preview_format:f.get('preview_format')},cache_read:scenario==='normal'&&f.has('cache_read')};if(scenario==='hang_step')data.step_timeout_seconds=15;const d=await api('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify(data)});location.href='/tasks/'+d.task_id;}catch(err){toast(err.message);button.disabled=false;}});
 $('#status-filter').addEventListener('change',()=>{browsingOlder=false;renderList().catch(e=>toast(e.message));});
 $('#more-tasks').addEventListener('click',()=>renderList(true).catch(e=>toast(e.message)));
}else {$('#event-step').addEventListener('change',resetEvents);$('#event-attempt').addEventListener('change',resetEvents);$('#older-events').addEventListener('click',()=>loadOlderEvents().catch(e=>toast(e.message)));$('#resume').addEventListener('click',()=>control('resume'));$('#cancel').addEventListener('click',()=>control('cancel'));}
function seconds(n) {return n == null ? '未记录' : `${Number(n).toFixed(1)}s`;}
function renderCheckpointSummary(x) {
 const cp=x.checkpoints;
 if(cp?.mode==='whole_step_cache'){$('#checkpoints').innerHTML='<div class="attempt"><b>整步结果复用</b><p>本次没有执行分块计算；直接复用完整已验证产物。来源见下方产物区。</p></div>';return;}
 $('#checkpoints').innerHTML=cp?`<div class="attempt"><b>步骤内部检查点 · ${cp.partition.version}</b><p>已提交 ${cp.completed}/${cp.total} 块 · 当前块 ${cp.current_chunk==null?'全部完成':cp.current_chunk+1} · 本次复用 ${cp.reused} 块</p>${cp.items.map(c=>`<p>${c.reused?'↻ 复用':'✓ 新算'} 块 ${c.index+1} · 帧 [${c.start}, ${c.end})<br>来源 ${esc(c.source_attempt.slice(0,8))} · 代次 ${c.epoch} · ${c.valid?'有效提交':'已失效'}</p>`).join('')}</div>`:'';
}
function initializeEventFilters(t) {
 if($('#event-step').options.length>1)return;
 $('#event-step').innerHTML='<option value="">全部步骤</option>'+t.steps.map(s=>`<option value="${esc(s.key)}">${esc(s.spec.name)}</option>`).join('');
 updateAttemptOptions(t);
}
function updateAttemptOptions(t) {
 const current=$('#event-attempt').value;
 const options=t.steps.flatMap(s=>s.attempts.map(a=>`<option value="${esc(a.id)}">${esc(s.spec.name)} · #${a.number} / 代次 ${a.epoch}</option>`));
 $('#event-attempt').innerHTML='<option value="">全部尝试</option>'+options.join('');
 $('#event-attempt').value=current;
}
function eventQuery() {return new URLSearchParams({limit:'30',...($('#event-step').value?{step_key:$('#event-step').value}:{}),...($('#event-attempt').value?{attempt_id:$('#event-attempt').value}:{})});}
function showEvents() {
 $('#event-count').textContent=`${eventRows.length} 条已加载`;
 $('#events').innerHTML=[...eventRows].sort((a,b)=>b.seq-a.seq).map(e=>`<div class="timeline-item"><strong>${esc(e.type)}</strong><small>#${e.seq} · ${stamp(e.at)} · 第 ${e.epoch} 代次${e.step_key?' · '+esc(e.step_key):''}</small><small>${esc(JSON.stringify(e.payload))}</small></div>`).join('')||'<p class="muted">该筛选下没有事件。</p>';
}
async function resetEvents() {eventInitialized=false;eventRows=[];eventNewest=0;eventOldest=null;try{await syncEvents();}catch(e){toast(e.message);}}
async function syncEvents() {
 if(eventLoading)return;
 eventLoading=true;
 const signature=$('#event-step').value+'|'+$('#event-attempt').value;
 try {
  updateAttemptOptions(snapshot);
  const q=eventQuery();
  if(!eventInitialized)q.set('before_seq',snapshot.event_cursor+1);else q.set('after_seq',eventNewest);
  const d=await api(`/api/tasks/${snapshot.id}/events?${q}`);
  if(signature!==$('#event-step').value+'|'+$('#event-attempt').value)return;
  const merged=new Map(eventRows.map(e=>[e.id,e]));d.items.forEach(e=>merged.set(e.id,e));eventRows=[...merged.values()];
  if(!eventInitialized){eventOldest=d.older_cursor;$('#older-events').hidden=!d.has_more;eventInitialized=true;}
  eventNewest=Math.max(eventNewest,...d.items.map(e=>e.seq),d.has_more?0:snapshot.event_cursor);
  showEvents();
 }finally{eventLoading=false;}
}
async function loadOlderEvents() {
 if(eventLoading||!eventOldest)return;
 eventLoading=true;
 const signature=$('#event-step').value+'|'+$('#event-attempt').value;
 try {
  const q=eventQuery();q.set('before_seq',eventOldest);
  const d=await api(`/api/tasks/${snapshot.id}/events?${q}`);
  if(signature!==$('#event-step').value+'|'+$('#event-attempt').value)return;
  const merged=new Map(eventRows.map(e=>[e.id,e]));d.items.forEach(e=>merged.set(e.id,e));eventRows=[...merged.values()];
  eventOldest=d.older_cursor;$('#older-events').hidden=!d.has_more;showEvents();
 }finally{eventLoading=false;}
}
async function renderHistory(resources) {
 const history=await api('/api/resources/history?limit=240');
 for(const slot of resources.slots) {
  const card=[...document.querySelectorAll('.resource')].find(e=>e.dataset.slot===slot.id);
  if(!card)continue;
  const values=history.items.filter(x=>x.slot_id===slot.id).slice(0,48).reverse();
  const valid=values.filter(x=>x.sample.valid && x.sample.utilization_percent!=null);
  // Missing samples break the line; they are never painted as zero utilization.
  const segments=[];let segment=[];
  values.forEach((x,i)=>{if(!x.sample.valid||x.sample.utilization_percent==null){if(segment.length)segments.push(segment);segment=[];}else segment.push(`${i*240/Math.max(values.length-1,1)},${54-x.sample.utilization_percent*.5}`);});
  if(segment.length)segments.push(segment);
  const graphic=segments.map(points=>`<polyline points="${points.join(' ')}" fill="none" stroke="#5eead4" stroke-width="2"/>`).join('');
  card.insertAdjacentHTML('beforeend',`<div class="trend"><svg viewBox="0 0 240 60" role="img" aria-label="MOCK资源利用率历史">${graphic}</svg><small>MOCK 历史 · ${values.length} 点 / ${valid.length} 有效 · 原始 ${history.policy.raw_hours}h，分钟汇总 ${history.policy.minute_days}d</small></div>`);
 }
}
refresh();
