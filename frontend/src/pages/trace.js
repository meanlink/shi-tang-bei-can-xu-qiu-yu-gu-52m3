import {jobs} from '../api/entities.js';
import {trace} from '../api/trace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,date,fmt,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
import {TraceState,RecallStatus,RecoveryStatus,BatchOrigin,Inspection} from '../constants/traceEnums.js';

let selectedBatch=null;
let filter='';

const badge=(value,labels)=>'<span class="trace-badge '+esc(value)+'">'+esc(labels[value]||value)+'</span>';

function approvedOptions(allJobs){
  const options=[];
  for(const job of allJobs){
    if(job.status!=='completed'||!job.approved)continue;
    for(const row of job.result?.table?.rows||[]){
      if(!row.date)continue;
      const label=job.name+' · '+row.date;
      options.push('<option value="'+job.id+'|'+esc(row.date)+'">'+esc(label)+'</option>');
    }
  }
  return options;
}

function batchOptions(batches,selected){
  return '<option value="">选择批次</option>'+batches.map(b=>'<option value="'+b.id+'" '+(String(b.id)===String(selected)?'selected':'')+'>'+esc(b.code+' · '+b.name)+'</option>').join('');
}

function stateTabs(counts){
  const entries=[['','全部',counts.batches],['pending','待追溯',counts.pending_batches],['recovering','追缴中',counts.recovering_batches],['settled','已结清',counts.settled_batches]];
  return '<div class="trace-tabs">'+entries.map(([key,label,count])=>'<button class="'+(filter===key?'active':'')+'" data-filter="'+key+'">'+esc(label)+'<small>'+count+'</small></button>').join('')+'</div>';
}

function chainList(rows,empty){
  if(!rows.length)return '<p class="empty-inline">'+esc(empty)+'</p>';
  return '<ol class="chain">'+rows.map(row=>'<li style="--depth:'+row.depth+'"><span class="chain-line"></span><strong>'+esc(row.code||('#'+row.batch_id))+'</strong><span>'+esc(row.name||'')+'</span><small>'+esc(BatchOrigin[row.origin]||row.origin)+' · '+row.qty+' 份 · '+esc(Inspection[row.inspection]||row.inspection)+'</small></li>').join('')+'</ol>';
}

function sourceRows(batches){
  return [0,1,2].map(i=>'<div class="source-row"><select name="source-'+i+'">'+batchOptions(batches,'')+'</select><input type="number" name="source-qty-'+i+'" min="1" placeholder="份数"><button type="button" class="text-button" data-clear-source="'+i+'">清空</button></div>').join('');
}

function recallCard(recall){
  return '<article class="recall-card '+esc(recall.stage)+'"><div><strong>召回 #'+recall.id+' · '+esc(recall.batch_code||('#'+recall.batch_id))+'</strong><p>'+esc(recall.reason||'检验不合格召回')+'</p><small>冻结 '+recall.frozen_count+' 批 · 追缴单 '+recall.order_count+' 张 · 应追 '+recall.original_qty+' 份 · 待追 '+recall.outstanding_qty+' 份 · '+date(recall.created_at)+'</small></div><div class="recall-meta">'+badge(recall.stage,TraceState)+badge(recall.status,RecallStatus)+(recall.status!=='completed'?'<button class="button small" data-resume="'+recall.id+'">恢复清查 ↗</button>':'')+'</div></article>';
}

function orderCard(order,canWrite){
  return '<article class="order-card '+esc(order.status)+'" data-order="'+order.id+'"><div class="order-head"><div><strong>追缴单 #'+order.id+' · '+esc(order.receiving_point)+'</strong><p>餐批 '+esc(order.batch_code||('#'+order.batch_id))+' · 原送出 '+order.original_qty+' 份 · 已追 '+order.recovered_qty+' 份 · 待追 '+order.outstanding_qty+' 份</p><small>交接 '+(order.handed_at?date(order.handed_at):'—')+' · '+esc(order.operator||'')+'</small></div>'+badge(order.status,RecoveryStatus)+'</div>'+(order.entries?.length?'<ul class="entry-list">'+order.entries.map(e=>'<li>+'+e.qty+' 份 · '+esc(e.actor)+' · '+date(e.at)+(e.note?' · '+esc(e.note):'')+'</li>').join('')+'</ul>':'')+(order.status!=='settled'&&canWrite?'<form class="inline-form entry-form" data-order-id="'+order.id+'"><input type="number" name="qty" min="1" max="'+order.outstanding_qty+'" placeholder="本次追缴份数" required><input name="note" placeholder="备注"><button type="submit" class="button small">登记追缴</button></form>':'')+'</article>';
}

function detailPanel(batch,chain,canWrite,isAdmin){
  const handovers=batch.handovers||[];
  return '<section class="panel trace-detail"><div class="section-title"><h2>'+esc(batch.code+' · '+batch.name)+'</h2><span>'+badge(batch.trace_state,TraceState)+' '+badge(batch.inspection,Inspection)+' '+(batch.frozen?'<span class="trace-badge frozen">已冻结</span>':'')+'</span></div><div class="trace-facts"><div><span>备餐日期</span><strong>'+esc(batch.meal_date)+'</strong></div><div><span>来源</span><strong>'+esc(BatchOrigin[batch.origin]||batch.origin)+'</strong></div><div><span>实备份数</span><strong>'+batch.actual_qty+'</strong></div><div><span>已拆出</span><strong>'+batch.outgoing_qty+'</strong></div><div><span>已交接</span><strong>'+batch.handed_qty+'</strong></div><div><span>剩余</span><strong>'+batch.remaining_qty+'</strong></div>'+(batch.suggested_qty!=null?'<div><span>建议份数</span><strong>'+batch.suggested_qty+'</strong></div>':'')+'</div>'+(batch.inspection_note?'<p class="hint">检验备注：'+esc(batch.inspection_note)+'</p>':'')+'<div class="two-columns trace-columns"><div><div class="section-title"><h2>完整来源链</h2><span>上游 → 本批 → 下游</span></div><h3 class="subhead">上游来源</h3>'+chainList(chain?.ancestors||[],'无上游，直接来自已批准建议。')+'<h3 class="subhead">本批来源构成</h3>'+(batch.sources?.length?'<ul class="source-list">'+batch.sources.map(s=>'<li>'+esc(s.code||('#'+s.batch_id))+' · '+esc(s.name||'')+' <strong>'+s.qty+' 份</strong></li>').join('')+'</ul>':'<p class="empty-inline">实备批次，无拆分来源。</p>')+'<h3 class="subhead">下游去向</h3>'+chainList(chain?.descendants||[],'尚无拆分或混装去向。')+'</div><div><div class="section-title"><h2>交接记录</h2><span>'+handovers.length+' 条原交接事实</span></div>'+(handovers.length?'<ul class="handover-list">'+handovers.map(h=>'<li><strong>'+esc(h.receiving_point)+'</strong><span>'+h.qty+' 份 · '+date(h.handed_at)+' · '+esc(h.operator)+'</span>'+(h.recovery_order_id?'<small>追缴单 #'+h.recovery_order_id+'</small>':'')+'</li>').join('')+'</ul>':'<p class="empty-inline">尚未登记交接。</p>')+(canWrite&&!batch.frozen?'<form class="inline-form" id="handover-form" data-batch="'+batch.id+'"><input name="receiving_point" placeholder="收餐点" required><input type="number" name="qty" min="1" max="'+batch.remaining_qty+'" placeholder="份数" required><input name="operator" placeholder="交接人（默认当前用户）"><input type="datetime-local" name="handed_at"><button type="submit" class="button small">登记交接</button></form>':'')+(batch.frozen?'<p class="hint">该批已冻结，不接受新的拆分、混装或交接。</p>':'')+'</div></div>'+(isAdmin?'<div class="inspection-bar"><div><strong>检验结论</strong><span>不合格将自动冻结本批及全部后代，并为已送出交接逐收餐点建立追缴单。</span></div><form class="inline-form" id="inspection-form" data-batch="'+batch.id+'"><input name="note" placeholder="检验备注（可选）"><input name="request_key" value="insp-'+batch.id+'-'+Math.random().toString(36).slice(2,8)+'" title="原请求重试时保留此键，即可沿用同一召回与追缴单"><select name="result"><option value="passed">合格</option><option value="failed">不合格</option><option value="pending">回到待检验</option></select><button type="submit" class="button small">记录检验</button></form></div>':'')+'</section>';
}

export async function tracePage(){
  const [overview,allBatches,recalls,orders,allJobs]=await Promise.all([
    trace.overview(),trace.batches(),trace.recalls(),trace.orders(),jobs.list()
  ]);
  const batches=filter?allBatches.filter(b=>b.trace_state===filter):allBatches;
  const canWrite=session.user.role!=='viewer',isAdmin=session.user.role==='admin';
  if(selectedBatch&&!batches.some(b=>b.id===selectedBatch))selectedBatch=batches[0]?.id||null;
  if(!selectedBatch&&batches.length)selectedBatch=batches[0].id;
  const detail=selectedBatch?await trace.batch(selectedBatch):null;
  const chain=selectedBatch?await trace.chain(selectedBatch):null;
  const pendingOrders=orders.filter(o=>o.status!=='settled');
  const suggestions=approvedOptions(allJobs);
  return heading('06 / TRACE & RECALL','成品追溯与召回去向','从已批准建议登记实备餐批，拆分或混装登记来源与份数；检验不合格沿全部后代冻结，按收餐点逐单追缴并保留原交接事实。','<button class="button" id="refresh-trace">刷新追溯 ↻</button>')
  +'<section class="trace-kpis">'+[['餐批',overview.batches],['已冻结',overview.frozen_batches],['待追溯',overview.pending_batches],['追缴中',overview.recovering_batches],['已结清',overview.settled_batches],['待追份数',overview.outstanding_qty]].map(([k,v])=>'<div><span>'+k+'</span><strong>'+fmt(v)+'</strong></div>').join('')+'</section>'
  +'<div class="trace-grid"><section class="panel"><div class="section-title"><h2>登记实备餐批</h2><span>来自已批准建议</span></div><form id="prepared-form"><label>已批准建议与日期<select name="suggestion" required>'+(suggestions.join('')||'<option value="">暂无已批准建议</option>')+'</select></label><label>餐批名称<input name="name" required minlength="2" placeholder="例如：周三午餐实备"></label><label>餐批编号<input name="code" placeholder="留空自动生成"></label><label>实际备餐份数<input type="number" name="actual_qty" min="1" required></label><button type="submit" class="primary" '+(canWrite&&suggestions.length?'':'disabled')+'>登记实备批</button><p class="hint">实备份数可与建议份数不同，后续拆分、混装与交接共用该实备上限。'+(suggestions.length?'':'请先在结果复核准批准一条建议。')+'</p></form></section>'
  +'<section class="panel"><div class="section-title"><h2>登记拆分 / 混装</h2><span>记录来源与份数</span></div><form id="derived-form"><label>类型<select name="origin"><option value="split">拆分</option><option value="mixed">混装</option></select></label><label>目标餐批名称<input name="name" required minlength="2" placeholder="例如：A 区拆分盒餐"></label><div class="parameter-grid"><label>备餐日期<input type="date" name="meal_date" required></label><label>目标编号<input name="code" placeholder="留空自动生成"></label></div><div class="section-title"><h2>来源餐批</h2><span>来源总用量不得超过实备</span></div><div class="source-rows">'+sourceRows(batches)+'</div><button type="submit" class="primary" '+(canWrite?'':'disabled')+'>登记来源关联</button><p class="hint">来源与目标构成有向无环图；已冻结批次不能继续拆分或混装。</p></form></section></div>'
  +'<section class="panel"><div class="section-title"><h2>餐批追溯台账</h2><span>'+batches.length+' 批 · 按追溯状态筛选</span></div>'+stateTabs(overview)+'<div class="batch-table"><div class="table-wrap"><table><thead><tr><th>餐批</th><th>日期</th><th>来源</th><th>实备</th><th>已拆出</th><th>已交接</th><th>检验</th><th>追溯状态</th><th></th></tr></thead><tbody>'+batches.map(b=>'<tr class="'+(b.id===selectedBatch?'selected':'')+'"><td><strong>'+esc(b.code)+'</strong><br><small>'+esc(b.name)+'</small></td><td>'+esc(b.meal_date)+'</td><td>'+esc(BatchOrigin[b.origin]||b.origin)+'</td><td>'+b.actual_qty+'</td><td>'+b.outgoing_qty+'</td><td>'+b.handed_qty+'</td><td>'+badge(b.inspection,Inspection)+'</td><td>'+badge(b.trace_state,TraceState)+(b.frozen?' <span class="trace-badge frozen">冻结</span>':'')+'</td><td><button class="text-button" data-batch-select="'+b.id+'">查看 ↗</button></td></tr>').join('')+'</tbody></table></div>'+(batches.length?'':'<p class="empty-inline">暂无餐批。</p>')+'</div></section>'
  +(detail?detailPanel(detail,chain,canWrite,isAdmin):'')
  +'<div class="trace-grid"><section class="panel"><div class="section-title"><h2>召回记录</h2><span>'+recalls.length+' 起 · 可按 request_key 重试</span></div>'+(recalls.length?recalls.map(recallCard).join(''):'<p class="empty-inline">暂无召回。检验不合格会在此生成召回与追缴单。</p>')+'</section><section class="panel"><div class="section-title"><h2>追缴单</h2><span>'+pendingOrders.length+' 张未结清</span></div>'+(orders.length?orders.map(o=>orderCard(o,canWrite)).join(''):'<p class="empty-inline">召回后按收餐点自动生成追缴单。</p>')+'</section></div>';
}

function collectSources(form){
  const sources=[];
  for(let i=0;i<3;i++){
    const batchId=form.querySelector('[name=source-'+i+']')?.value;
    const qty=Number(form.querySelector('[name=source-qty-'+i+']')?.value||0);
    if(batchId&&qty>0)sources.push({batch_id:Number(batchId),qty});
  }
  return sources;
}

export function bindTrace(refresh){
  document.querySelector('#refresh-trace')?.addEventListener('click',refresh);
  document.querySelectorAll('[data-filter]').forEach(button=>button.onclick=()=>{filter=button.dataset.filter;refresh();});
  document.querySelectorAll('[data-batch-select]').forEach(button=>button.onclick=()=>{selectedBatch=Number(button.dataset.batchSelect);refresh();});
  document.querySelectorAll('[data-clear-source]').forEach(button=>button.onclick=()=>{const i=button.dataset.clearSource;document.querySelector('[name=source-'+i+']').value='';document.querySelector('[name=source-qty-'+i+']').value='';});
  const prepared=document.querySelector('#prepared-form');
  if(prepared)submit(prepared,async f=>{
    const [jobId,mealDate]=String(f.get('suggestion')).split('|');
    const batch=await trace.createBatch({name:f.get('name'),code:f.get('code')||'',meal_date:mealDate,origin:'prepared',job_id:Number(jobId),actual_qty:Number(f.get('actual_qty'))});
    selectedBatch=batch.id;notify('实备餐批已登记');await refresh();
  });
  const derived=document.querySelector('#derived-form');
  if(derived)submit(derived,async f=>{
    const sources=collectSources(derived);
    if(!sources.length)throw new Error('请至少登记一个来源餐批及份数');
    const batch=await trace.createBatch({name:f.get('name'),code:f.get('code')||'',meal_date:f.get('meal_date'),origin:f.get('origin'),sources});
    selectedBatch=batch.id;notify('拆分/混装来源已登记');await refresh();
  });
  const handover=document.querySelector('#handover-form');
  if(handover)submit(handover,async f=>{
    const raw=f.get('handed_at');
    await trace.handover(Number(handover.dataset.batch),{
      receiving_point:f.get('receiving_point'),qty:Number(f.get('qty')),operator:f.get('operator')||'',
      handed_at:raw?new Date(raw).getTime()/1000:null
    });
    notify('交接已登记');await refresh();
  });
  const inspection=document.querySelector('#inspection-form');
  if(inspection)submit(inspection,async f=>{
    const key=f.get('request_key')||('ui:'+inspection.dataset.batch+':'+Date.now());
    const result=await trace.inspect(Number(inspection.dataset.batch),{result:f.get('result'),note:f.get('note')||'',request_key:key});
    notify(result.recall?'检验已记录，召回已完成清查':'检验结论已记录');await refresh();
  });
  document.querySelectorAll('[data-resume]').forEach(button=>button.onclick=async()=>{try{await trace.resumeRecall(button.dataset.resume);notify('已续跑召回清查');await refresh();}catch(e){notify(e.message,true);}});
  document.querySelectorAll('.entry-form').forEach(form=>submit(form,async f=>{
    await trace.addEntry(Number(form.dataset.orderId),{qty:Number(f.get('qty')),note:f.get('note')||''});
    notify('追缴已登记');await refresh();
  }));
}
