'use strict';
const $ = id => document.getElementById(id);
let token = '', current = '', state = null, timer = null, reviewKey = '';
const starter = 'https://docs.langchain.com/oss/python/langgraph/overview\nhttps://docs.langchain.com/oss/python/langchain/overview';
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-App-Token':token},body:JSON.stringify(body)});
  const value = await r.json();
  if (!r.ok) throw new Error(value.error || 'The request could not finish.');
  return value;
}
function alertError(error) { $('alert').textContent = error.message; $('alert').hidden = false; }
function node(tag, text, className) { const n = document.createElement(tag); if(text !== undefined) n.textContent = text; if(className) n.className = className; return n; }
async function history() {
  const runs = await api('/api/runs'); $('history').replaceChildren();
  runs.forEach(run => { const b=node('button',run.question); b.classList.toggle('selected',run.id===current); b.onclick=()=>openRun(run.id).catch(alertError); $('history').append(b); });
  if (!runs.length) $('history').append(node('p','Your first brief starts here.','hint'));
}
async function openRun(id) { current=id; reviewKey=''; $('composer').hidden=true; $('run').hidden=false; $('alert').hidden=true; await poll(); await history(); }
async function poll() {
  clearTimeout(timer); if(!current) return;
  state=await api('/api/runs/'+current); render();
  if(['starting','planning','gathering','verifying','drafting'].includes(state.status)) timer=setTimeout(()=>poll().catch(alertError),1400);
}
function render() {
  $('run-question').textContent=state.question;
  const names={starting:'Starting',planning:'Planning',gathering:'Gathering evidence',verifying:'Checking evidence',awaiting_review:'Your review is needed',complete:'Brief ready',cancelled:'Cancelled',failed:'Needs attention',drafting:'Creating brief'};
  $('status').textContent=names[state.status]||state.status;
  const index={starting:0,planning:0,gathering:1,verifying:2,awaiting_review:3,drafting:4,complete:5,cancelled:3,failed:-1}[state.status]??0;
  document.querySelectorAll('#steps li').forEach((n,i)=>{n.classList.toggle('done',i<index);n.classList.toggle('active',i===index);});
  const events=state.events||[];
  $('activity').textContent=events.length ? (events[events.length-1].message||String(events[events.length-1])) : 'Starting a saved research workflow…';
  $('claims').replaceChildren(); const claims=state.claims||[];
  $('evidence-count').textContent=claims.length+' VERIFIED CLAIM'+(claims.length===1?'':'S');
  claims.forEach(c=>{const box=node('div',undefined,'claim');box.append(node('p',c.claim),node('blockquote',c.quote),node('small','✓ Quote matched · '+c.source_id));$('claims').append(box);});
  if(!claims.length) $('claims').append(node('p','Verified claims will appear here as the workflow progresses.','claim hint'));
  $('sources').replaceChildren(); (state.sources||[]).forEach(s=>{const a=node('a',(s.id||'')+' · '+s.title+(s.error?' · unavailable':''),'source'); if(/^https:\/\//.test(s.url)){a.href=s.url;a.target='_blank';a.rel='noopener noreferrer';} $('sources').append(a);});
  const waiting=state.status==='awaiting_review'; $('review-form').hidden=!waiting; $('review-wait').hidden=waiting||['complete','cancelled','failed'].includes(state.status); $('review-done').hidden=!['complete','cancelled','failed'].includes(state.status);
  if(waiting && reviewKey!==current){ $('outline').value=(state.outline||[]).join('\n');reviewKey=current; }
  $('review-done').textContent=state.status==='complete'?'✓ Approved. The graph resumed from its saved checkpoint and assembled your cited brief.':state.status==='cancelled'?'This run was cancelled at the review step. Its evidence remains saved.':'The evidence gate or workflow could not finish. '+(state.errors||[]).join(' ');
  $('result').hidden=state.status!=='complete';$('brief').textContent=state.brief||'';
  if(state.status==='failed'){ $('alert').textContent=(state.errors||['The workflow could not finish.']).join(' ');$('alert').hidden=false; }
}
$('research-form').onsubmit=async e=>{e.preventDefault();$('alert').hidden=true;$('start').disabled=true;try{const result=await api('/api/runs',{question:$('question').value,urls:$('urls').value.split('\n').map(x=>x.trim()).filter(Boolean),mode:$('mode').value});await openRun(result.id);}catch(error){alertError(error);}finally{$('start').disabled=false;}};
async function review(approved){$('approve').disabled=true;$('reject').disabled=true;try{await api('/api/runs/'+current+'/review',{approved,outline:$('outline').value.split('\n').map(x=>x.trim()).filter(Boolean)});timer=setTimeout(()=>poll().catch(alertError),300);}catch(error){alertError(error);}finally{$('approve').disabled=false;$('reject').disabled=false;}}
$('review-form').onsubmit=e=>{e.preventDefault();review(true);};$('reject').onclick=()=>review(false);
$('new-run').onclick=()=>{clearTimeout(timer);current='';reviewKey='';$('composer').hidden=false;$('run').hidden=true;$('alert').hidden=true;history().catch(alertError);$('question').focus();};
$('mode').onchange=()=>{ $('mode-note').textContent=$('mode').value==='demo'?'A reproducible sample with labeled documentation excerpts. Every step runs through the real graph.':'Fetch public HTTPS pages and extract evidence with qwen3:4b on local Ollama. Start Ollama first. No API key is needed; page hosts receive your requests.'; if($('mode').value==='demo') $('urls').value=starter; else if($('urls').value===starter) $('urls').value=starter.replaceAll('/overview','/overview.md'); };
$('download').onclick=()=>{const url=URL.createObjectURL(new Blob([state.brief],{type:'text/markdown;charset=utf-8'}));const a=node('a');a.href=url;a.download='researchflow-brief.md';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
(async()=>{try{token=(await api('/api/config')).token;await history();}catch(error){alertError(error);}})();
