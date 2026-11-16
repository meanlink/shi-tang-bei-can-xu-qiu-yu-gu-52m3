import {api} from './client.js';
export const trace={
 overview:()=>api('/trace/overview'),
 batches:state=>api('/trace/batches'+(state?'?state='+encodeURIComponent(state):'')),
 batch:id=>api('/trace/batches/'+id),
 chain:id=>api('/trace/batches/'+id+'/chain'),
 createBatch:data=>api('/trace/batches',{method:'POST',body:JSON.stringify(data)}),
 addSources:(id,sources)=>api('/trace/batches/'+id+'/sources',{method:'POST',body:JSON.stringify({sources})}),
 handover:(id,data)=>api('/trace/batches/'+id+'/handovers',{method:'POST',body:JSON.stringify(data)}),
 inspect:(id,data)=>api('/trace/batches/'+id+'/inspection',{method:'POST',body:JSON.stringify(data)}),
 requestRecall:(id,data)=>api('/trace/batches/'+id+'/recalls',{method:'POST',body:JSON.stringify(data)}),
 recalls:()=>api('/trace/recalls'),
 recall:id=>api('/trace/recalls/'+id),
 resumeRecall:id=>api('/trace/recalls/'+id+'/resume',{method:'POST'}),
 orders:status=>api('/trace/orders'+(status?'?status='+encodeURIComponent(status):'')),
 order:id=>api('/trace/orders/'+id),
 addEntry:(id,data)=>api('/trace/orders/'+id+'/entries',{method:'POST',body:JSON.stringify(data)}),
 handovers:()=>api('/trace/handovers')
};
