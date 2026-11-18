import {api} from './client.js';
export const sandboxAPI={
 list:()=>api('/sandbox'),
 writeOrder:order_no=>api('/sandbox/orders',{method:'POST',body:JSON.stringify({order_no})}),
 correct:(id,order_no)=>api('/sandbox/orders/'+id+'/correct',{method:'POST',body:JSON.stringify({order_no})}),
 startBuild:attempt_key=>api('/sandbox/builds',{method:'POST',body:JSON.stringify({attempt_key})}),
 tick:(build_id,steps=1)=>api('/sandbox/builds/'+build_id+'/tick',{method:'POST',body:JSON.stringify({steps})}),
 interrupt:build_id=>api('/sandbox/builds/'+build_id+'/interrupt',{method:'POST',body:JSON.stringify({})}),
 failover:(attempt_key,target)=>api('/sandbox/failovers',{method:'POST',body:JSON.stringify({attempt_key,target})}),
 replicaCheck:order_no=>api('/sandbox/replica-check',{method:'POST',body:JSON.stringify({order_no})}),
 reset:()=>api('/sandbox/reset',{method:'POST',body:JSON.stringify({})})
};
