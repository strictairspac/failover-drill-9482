import {store} from '../stores/entities.js';
import {session} from '../stores/session.js';
import {sandboxAPI} from '../api/sandbox.js';
import {table} from '../components/common/table.js';
import {esc} from '../utils/html.js';
import {message} from '../hooks/action.js';

const BUILD_TEXT={queued:'排队中',building:'构建中',paused:'等待处理',active:'已生效',failed:'失败'};
const FO_TEXT={deferred:'暂缓',completed:'已放行'};

function fmt(value){if(!value)return '—';const d=String(value).replace(' ','T');const date=new Date(d.endsWith('Z')?d:d+'Z');if(isNaN(date))return esc(value);return date.toLocaleString('zh-CN',{hour12:false});}

function stageFlow(build){
 if(!build)return '<p class="readonly">还没有构建记录。填写构建标识后开始在线建索引。</p>';
 const interrupted=build.stage==='interrupted';
 const waiting=build.stage==='awaiting_correction';
 const activeStage=(interrupted||waiting)?(build.resume_stage||'scanning'):build.stage;
 const order=['scanning','catching_up','validating','active'];
 const labels={scanning:'① 扫描存量',catching_up:'② 追平构建期写入',validating:'③ 校验一致',active:'④ 索引生效'};
 return `<div class="flow">${order.map((s,i)=>{
  const done=activeStage==='active'?true:order.indexOf(activeStage)>i;
  const current=activeStage===s;
  const blocked=current&&(waiting||interrupted);
  return `<div class="step ${done?'done':''} ${current?'current':''} ${blocked?'blocked':''}"><b>${labels[s]}</b><span>${done?'完成':current?(blocked?(interrupted?'构建中断':'等待纠正旧重单'):'进行中'):'未开始'}</span></div>`;
 }).join('')}</div>`;
}

function progressBar(build){
 if(!build)return '';
 const pct=Math.max(0,Math.min(100,build.progress));
 return `<div class="progress"><div class="bar"><span style="width:${pct}%"></span></div><p>${build.scanned} / ${build.total_at_start} 条已扫描 · 影子索引 ${build.index_entries} 条 · 待核对写入 ${build.pending_write_logs} 条</p></div>`;
}

export function sandboxPage(){
 const s=store.sandbox||{};
 const build=s.latest_build;
 const replica=s.replica||{};
 const writable=session.role!=='viewer';
 return `<section class="split sandbox-block"><div><h2>在线建唯一索引</h2><p>存量按批次扫描进影子唯一索引。遇到旧重单先停下等纠正；构建期间的写入照常落库并记日志，追平后逐条核对。旧重单清完、写入核对齐，索引才生效。</p><p class="big-note">索引未生效，副本接不住防重。</p></div>
 <div class="card stage-card"><div class="section-title"><h2>构建进度 <small>${build?'#'+build.id+' · '+esc(build.attempt_key):''}</small></h2><span>${build?esc(BUILD_TEXT[build.status]||build.status):'未开始'}</span></div>${stageFlow(build)}${progressBar(build)}${build?`<p class="notice ${build.status==='paused'||build.status==='interrupted'?'error':''}">${esc(build.wait_reason||'')}</p>`:''}
 ${build&&build.index_effective_at?`<p class="checksum">索引生效时间：${esc(fmt(build.index_effective_at))}</p>`:''}
 ${writable?`<form id="build-form" class="inline"><label>构建标识<input name="attempt_key" value="${esc(build?build.attempt_key:'orders-uniq-20261118')}" maxlength="80" required></label><button type="submit">${build&&['building','paused'].includes(build.status)?'同次重试（恢复进度）':'开始构建'}</button></form>
 <div class="toolbar"><button class="secondary" id="tick" ${!build||['active','interrupted'].includes(build.status)?'disabled':''}>推进一批扫描</button><button class="secondary" id="tick-run" ${!build||['active','interrupted'].includes(build.status)?'disabled':''}>持续推进到停点</button><button class="secondary" id="interrupt" ${!build||!['building','paused'].includes(build.status)?'disabled':''}>模拟构建中断</button></div>
 ${build&&build.blocking_order_id?`<p class="hint">阻塞单号 #${build.blocking_order_id}：<a href="#" data-correct="${build.blocking_order_id}" data-order-no="${esc((s.orders||[]).find(o=>o.id===build.blocking_order_id)?.order_no||'')}">纠正这条旧重单</a></p>`:''}`:''}
 <p class="hint">同一次构建重试请沿用同一标识：只从游标处恢复，不会重复建索引；换了标识而旧构建未完则会被拒绝。</p></div></section>
 <section class="split sandbox-block"><div><h2>副本防重视图</h2><p class="notice ${replica.can_detect_duplicates?'':'error'}">${esc(replica.reason||'')}</p><dl class="facts"><div><dt>副本能否拒重</dt><dd>${replica.can_detect_duplicates?'能':'不能'}</dd></div><div><dt>影子索引覆盖</dt><dd>${esc(replica.coverage||'0')}</dd></div><div><dt>剩余旧重单组</dt><dd>${s.duplicates?.length??0}</dd></div><div><dt>切主闸门</dt><dd>${replica.can_detect_duplicates?'放行':'暂缓'}</dd></div></dl>
 <div class="toolbar"><form id="replica-form" class="inline"><label>单号<input name="order_no" value="HS-2611" maxlength="80" required></label><button class="secondary" type="submit">副本视角检查</button></form></div><div id="replica-result"></div></div>
 <div><h2>切主</h2><p>构建与切主并发时，先看副本能否按索引拒绝重复单号；接不住就暂缓，等索引生效后自动放行。</p>${writable?`<form id="failover-form"><label>切主标识<input name="attempt_key" value="fo-20261118" maxlength="80" required></label><label>目标副本<input name="target" value="hs-east-replica" maxlength="80" required></label><button type="submit">请求切主</button></form>`:'<p class="readonly">观察员可查看切主记录。</p>'}<div class="list">${(s.failovers||[]).slice(0,4).map(f=>`<div class="list-row"><b>${esc(f.attempt_key)} → ${esc(f.target)}</b><span class="badge ${f.status==='completed'?'completed':'failed'}">${esc(FO_TEXT[f.status]||f.status)}</span><p>${esc(f.wait_reason||'')}${f.resolved_at?' · 放行于 '+esc(fmt(f.resolved_at)):''}</p></div>`).join('')||'<p class="readonly">尚无切主请求。</p>'}</div></div></section>
 <section class="split sandbox-block"><div><h2>写单</h2><p>新单号照常写入；已有单号（包括还没纠正的旧重单）立即拒绝。构建期间的写入会记入待核对日志。</p>${writable?`<form id="order-form" class="inline"><label>单号<input name="order_no" value="HS-3001" maxlength="80" required></label><button type="submit">写入订单</button></form>`:'<p class="readonly">观察员只读。</p>'}${writable?`<form id="correct-form" class="inline"><label>订单 ID<input name="order_id" type="number" min="1" required></label><label>纠正后单号<input name="order_no" value="HS-2611-R" maxlength="80" required></label><button class="secondary" type="submit">纠正旧重单</button></form>`:''}<p class="hint">只有历史重复单号可纠正，且新单号不能被占用；其他订单保持原单号。</p></div>
 <div><h2>订单与旧重单</h2>${table(['#','单号','来源','状态','操作'],(s.orders||[]).map(o=>[o.id,esc(o.order_no),o.origin==='legacy'?'历史存量':'新写入',o.duplicate_of_id!==null&&o.corrected_at===null?'历史重单':o.duplicate_of_id!==null?'已纠正':'正常',o.can_correct&&writable?`<a href="#" data-correct="${o.id}" data-order-no="${esc(o.order_no)}">纠正 →</a>`:'—']))}</div></section>
 <div class="section-title"><h2>事件流</h2><span>记录构建停在哪里、何时生效</span></div><div class="timeline">${(s.events||[]).slice(0,18).map(e=>`<div class="event ${esc(e.kind)}"><time>${esc(fmt(e.created_at))}</time><p>${esc(e.message)}</p></div>`).join('')||'<p class="readonly">尚无事件。</p>'}</div>
 <div class="toolbar"><button class="secondary" id="refresh-sandbox">刷新沙盘状态</button>${session.role==='admin'?'<button class="secondary" id="reset-sandbox">重置演示数据</button>':''}</div>`;
}

export function bindSandbox(render){
 const refresh=()=>{location.hash='#sandbox';render();};
 let busy=false;
 async function run(action,{text='已完成，记录可复查',reload=true}={}){
  if(busy)return;busy=true;message('处理中…');
  try{await action();if(text)message(text);if(reload)await refresh();}
  catch(err){message(err.message,true);}
  finally{busy=false;}
 }
 document.querySelector('#refresh-sandbox')?.addEventListener('click',refresh);
 document.querySelector('#reset-sandbox')?.addEventListener('click',()=>run(async()=>{await sandboxAPI.reset();},{text:'演示数据已重置'}));
 document.querySelector('#build-form')?.addEventListener('submit',e=>{
  e.preventDefault();const key=e.target.elements.attempt_key.value.trim();
  run(async()=>{await sandboxAPI.startBuild(key);},{text:'已按标识启动/恢复构建'});
 });
 document.querySelector('#tick')?.addEventListener('click',()=>{
  const build=store.sandbox?.latest_build;if(!build)return;
  run(async()=>{await sandboxAPI.tick(build.id);},{text:'已推进一批扫描'});
 });
 document.querySelector('#tick-run')?.addEventListener('click',()=>{
  const build=store.sandbox?.latest_build;if(!build)return;
  run(async()=>{const r=await sandboxAPI.tick(build.id,20);message(r.status==='paused'?'构建停在：'+r.wait_reason:r.status==='active'?'唯一索引已生效':'已推进到'+r.stage_label);},{text:'',reload:true});
 });
 document.querySelector('#interrupt')?.addEventListener('click',()=>{
  const build=store.sandbox?.latest_build;if(!build)return;
  run(async()=>{await sandboxAPI.interrupt(build.id);},{text:'构建已中断，可同标识重试恢复'});
 });
 document.querySelector('#order-form')?.addEventListener('submit',e=>{
  e.preventDefault();const no=e.target.elements.order_no.value.trim();
  run(async()=>{await sandboxAPI.writeOrder(no);},{text:`订单 ${no} 已写入`});
 });
 document.querySelector('#correct-form')?.addEventListener('submit',e=>{
  e.preventDefault();const id=Number(e.target.elements.order_id.value);const no=e.target.elements.order_no.value.trim();
  run(async()=>{await sandboxAPI.correct(id,no);},{text:`订单 #${id} 已纠正为 ${no}`});
 });
 document.querySelectorAll('[data-correct]').forEach(el=>el.addEventListener('click',e=>{
  e.preventDefault();const id=el.dataset.correct;const no=el.dataset.orderNo+'-R';
  run(async()=>{await sandboxAPI.correct(Number(id),no);},{text:`旧重单 #${id} 已纠正为 ${no}`});
 }));
 document.querySelector('#failover-form')?.addEventListener('submit',e=>{
  e.preventDefault();const key=e.target.elements.attempt_key.value.trim();const target=e.target.elements.target.value.trim();
  run(async()=>{const row=await sandboxAPI.failover(key,target);message(row.status==='completed'?'切主已放行':'切主已暂缓：'+row.wait_reason);},{text:'',reload:false});
 });
 document.querySelector('#replica-form')?.addEventListener('submit',e=>{
  e.preventDefault();const no=e.target.elements.order_no.value.trim();
  run(async()=>{const r=await sandboxAPI.replicaCheck(no);const box=document.querySelector('#replica-result');if(box)box.innerHTML=`<p class="notice ${r.would_be_rejected_on_replica?'':'error'}">${esc(r.reason)}<br>${r.would_be_rejected_on_replica?'副本会拒绝该单号。':'副本接不住：该单号会被当成新单收下。'}（命中 ${r.orders_with_same_no?.length??0} 条）</p>`;},{text:'',reload:false});
 });
}
