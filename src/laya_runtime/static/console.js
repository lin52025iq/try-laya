'use strict';
const $ = id => document.getElementById(id);
let active = '', selected = null, packs = [], socket = null, lastSeq = 0, imageURL = null, refreshing = false;
try{$('token').value = sessionStorage.getItem('agent-token') || '';}catch{ /* Storage may be denied by browser policy. */ }
function message(text, error=false){$('message').textContent=text;$('message').style.color=error?'#ffabb9':'#8e9eb2';}
async function api(path, body, binary=false){
  const response=await fetch('/api/v1'+path,{method:body===undefined?'GET':'POST',headers:{'Authorization':'Bearer '+$('token').value,...(body===undefined?{}:{'Content-Type':'application/json'})},body:body===undefined?undefined:JSON.stringify(body)});
  if(!response.ok){let data;try{data=await response.json();}catch{data={detail:response.statusText};}throw new Error(JSON.stringify(data.detail));}
  return binary?response.blob():response.json();
}
function action(fn){return async()=>{try{await fn();}catch(error){message(error.message,true);}};}
function renderSession(s){selected=s;$('runstate').textContent=s.state.toUpperCase();$('owner').textContent=s.owner.toUpperCase();$('steps').textContent=s.steps;$('episodeId').textContent=s.episode;$('pending').textContent=s.pending?JSON.stringify({action:s.pending.template.id,kind:s.pending.template.kind,target:s.pending.template.target,value_ref:s.pending.template.value_ref},null,2):'没有待批准动作';$('approve').disabled=!s.pending;$('details').textContent=JSON.stringify({versions:s.versions,state_version:s.state_version,last_result:s.last_result},null,2);}
function appendEvent(e){
  if(!e.seq || e.seq<=lastSeq)return;
  lastSeq=e.seq; const row=document.createElement('div');row.className='event';
  const seq=document.createElement('span');seq.className='seq';seq.textContent='#'+e.seq;
  const content=document.createElement('div'),title=document.createElement('strong'),detail=document.createElement('p');
  title.textContent=e.type;detail.textContent=JSON.stringify(e.payload);content.append(title,detail);row.append(seq,content);$('events').append(row);
  while($('events').children.length>100)$('events').firstChild.remove();
  $('eventCount').textContent=lastSeq+' events';$('events').scrollTop=$('events').scrollHeight;
}
async function catchup(){if(!active)return;const events=await api('/sessions/'+active+'/events?after='+lastSeq+'&limit=500');events.forEach(appendEvent);}
async function chooseSession(id){
  if(socket)socket.close();active=id;lastSeq=0;$('events').replaceChildren();
  if(!active)return;renderSession(await api('/sessions/'+active));
  socket=new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/api/v1/ws/'+active);
  socket.onopen=()=>socket.send(JSON.stringify({token:$('token').value}));
  socket.onmessage=event=>{const e=JSON.parse(event.data);if(e.type==='STREAM_READY'){catchup().catch(error=>message(error.message,true));return;}if(e.seq>lastSeq+1){catchup().catch(()=>{});}else appendEvent(e);};
  await refresh();
}
async function refresh(){
  if(!active||refreshing)return;refreshing=true;
  try{const sid=active;const [s,blob]=await Promise.all([api('/sessions/'+sid),api('/sessions/'+sid+'/screenshot',undefined,true)]);if(sid!==active)return;renderSession(s);const next=URL.createObjectURL(blob);$('screen').src=next;$('screen').hidden=false;$('placeholder').style.display='none';if(imageURL)URL.revokeObjectURL(imageURL);imageURL=next;}
  finally{refreshing=false;}
}
async function loadProfiles(){packs=await api('/profiles');$('profile').replaceChildren();for(const p of packs){const option=document.createElement('option');option.value=p.id;option.textContent=p.name;$('profile').append(option);}}
$('connect').onclick=action(async()=>{try{sessionStorage.setItem('agent-token',$('token').value);}catch{}const health=await api('/health');$('provider').textContent=health.provider;$('provider').classList.toggle('demo',health.simulation);await loadProfiles();const sessions=await api('/sessions');$('sessions').replaceChildren(new Option('选择会话',''));for(const s of sessions)$('sessions').add(new Option(s.id,s.id));message(health.simulation?'DEMO：未使用 Laya 模型，仅测试执行流程。':'已连接；开始执行前请预热 Laya。');});
$('warmup').onclick=action(async()=>{message('正在加载所选模型…');const r=await api('/policy/warmup',{});message('预热完成：'+r.provider);});
$('create').onclick=action(async()=>{const runtime=$('runtime').value;const body={runtime_kind:runtime,profile_id:$('profile').value,instruction:$('goal').value,facts:JSON.parse($('facts').value),mode:$('mode').value,safety:{allow_auto_click:$('autoClick').checked,allow_realtime:$('realtime').checked}};if($('target').value)body[runtime==='browser'?'url':'serial']=$('target').value;const s=await api('/sessions',body);$('sessions').add(new Option(s.id,s.id));$('sessions').value=s.id;await chooseSession(s.id);message('会话已创建，尚未自动运行。');});
$('sessions').onchange=action(()=>chooseSession($('sessions').value));
for(const [id,route] of [['resume','resume'],['pause','pause'],['stop','stop'],['episode','episodes']])$(id).onclick=action(async()=>{if(!active)throw Error('先创建会话');renderSession(await api('/sessions/'+active+'/'+route,id==='resume'?{mode:$('mode').value}:{}));});
$('approve').onclick=action(async()=>{if(!selected?.pending)return;await api('/sessions/'+active+'/approve',{action_id:selected.pending.id});await refresh();});
$('goalUpdate').onclick=action(async()=>{if(!active)return;renderSession(await api('/sessions/'+active+'/goal',{instruction:$('goal').value,facts:JSON.parse($('facts').value)}));});
$('screen').onclick=async event=>{try{const r=$('screen').getBoundingClientRect();renderSession(await api('/sessions/'+active+'/human',{kind:'click',x:(event.clientX-r.left)/r.width,y:(event.clientY-r.top)/r.height}));await refresh();}catch(e){message(e.message,true);}};
$('sendText').onclick=action(async()=>{if(!active)return;renderSession(await api('/sessions/'+active+'/human',{kind:'text',text:$('manualText').value}));$('manualText').value='';await refresh();});
$('enter').onclick=action(async()=>{if(active){await api('/sessions/'+active+'/human',{kind:'key',key:'Enter'});await refresh();}});
$('snapshot').onclick=action(async()=>{if(active){const r=await api('/sessions/'+active+'/artifacts',{});message('截图已保存到本地 ArtifactStore：'+r.artifact_id);}});
$('profileFile').onchange=action(async()=>{const file=$('profileFile').files[0];if(file){if(file.size>65536)throw Error('策略包过大');$('profileJson').value=await file.text();}});
$('importProfile').onclick=action(async()=>{const p=JSON.parse($('profileJson').value);await api('/profiles',p);await loadProfiles();$('profile').value=p.id;message('策略包已保存，未激活。');});
$('activateProfile').onclick=action(async()=>{if(!active)throw Error('先选择会话');const p=JSON.parse($('profileJson').value);renderSession(await api('/sessions/'+active+'/profile',p));message('策略已更新，旧决策失效；确认后再恢复。');});
$('compile').onclick=action(async()=>{message('生成策略草稿中；不会修改当前执行策略。');const r=await api('/strategist/compile',{profile_id:$('profile').value,goal:$('goal').value,guide:$('guide').value});$('profileJson').value=JSON.stringify(r.draft,null,2);message('策略草稿已生成，请检查后保存和应用。');});
setInterval(()=>{if(active)refresh().catch(error=>message(error.message,true));},650);
