'use strict';
const $ = id => document.getElementById(id);
const fmt = value => value === null ? 'Unavailable' : Number(value).toFixed(2);
const titles = {normal:'Normal trading',cloud:'Cloud shortfall',utility_limit:'Utility limit',preference_change:'Preference change'};
const descriptions = {
  normal:'Start with accurate forecasts. The meter outcomes match the available offers.',
  cloud:'A cloud reduces S1’s actual export after its forecast-based offer. Watch how settlement differs from commitment.',
  utility_limit:'The utility allows at most 5 kWh of trading in this interval. Approval happens before any commitment.',
  preference_change:'S1 keeps a larger reserve and raises its minimum selling price. The agent must respect both preferences.'
};
const stages = ['Observe','Propose','Validate','Commit','Meter','Settle'];
const eventKinds = ['energy_observe','energy_proposal','energy_validation','energy_commitment','energy_meter','energy_result'];
let presets = {}, selected = 'normal', latest = null, stage = 5, running = false;
const urls = [];
function text(id,value){$(id).textContent=value;}
function input(id,value,label,max,step='0.01',required=true){
  const el=document.createElement('input');el.id=id;el.type='number';el.min='0';el.max=String(max);el.step=step;el.required=required;el.value=value===null?'':String(value);el.setAttribute('aria-label',label);return el;
}
function tableRow(parent,values){const row=document.createElement('tr');values.forEach(value=>{const td=document.createElement('td');td.textContent=value;row.append(td)});parent.append(row);}
function populate(config){
  for(const group of ['sellers','buyers']){
    $(group).replaceChildren();
    const fields=group==='sellers'?[['forecast_surplus','forecast surplus',1000],['reserve','reserve',1000],['min_price','minimum selling price',100]]:[['demand','expected demand',1000],['max_price','maximum buying price',100],['budget','spending limit',100000]];
    config[group].forEach(person=>{const row=document.createElement('tr'),name=document.createElement('th');name.scope='row';name.textContent=person.name;row.append(name);fields.forEach(([key,label,max])=>{const td=document.createElement('td');td.append(input(person.name+'-'+key,person[key],person.name+' '+label,max));row.append(td)});$(group).append(row)});
  }
  $('cap').value=config.utility_cap_kwh;$('tariff').value=config.grid_tariff;$('missing-policy').value=config.missing_meter_policy;
  $('meters').replaceChildren();
  [...config.sellers,...config.buyers].forEach(person=>{const seller=person.name.startsWith('S'),key=seller?'actual_export':'actual_import',label=document.createElement('label');label.textContent=person.name+' actual '+(seller?'export':'import')+' (kWh)';label.append(input(person.name+'-actual',person[key],label.textContent,1000,'0.01',false));$('meters').append(label)});
}
function readInputs(){
  const num=id=>$(id).valueAsNumber;
  const actual=id=>$(id+'-actual').value===''?null:num(id+'-actual');
  return {sellers:['S1','S2','S3'].map(name=>({name,forecast_surplus:num(name+'-forecast_surplus'),reserve:num(name+'-reserve'),min_price:num(name+'-min_price'),actual_export:actual(name)})),buyers:['B1','B2','B3'].map(name=>({name,demand:num(name+'-demand'),max_price:num(name+'-max_price'),budget:num(name+'-budget'),actual_import:actual(name)})),utility_cap_kwh:num('cap'),grid_tariff:num('tariff'),missing_meter_policy:$('missing-policy').value};
}
function markDraft(){
  const dirty=!latest||JSON.stringify(readInputs())!==JSON.stringify(latest.inputs);
  $('stale').hidden=!latest||!dirty;
  text('draft-status',dirty?'Inputs ready to run.':'Inputs match the displayed run.');
}
function loadPreset(name){selected=name;populate(presets[name]);text('preset-description',descriptions[name]);[...$('presets').children].forEach(b=>{const active=b.dataset.name===name;b.classList.toggle('active',active);b.setAttribute('aria-pressed',active)});$('error').hidden=true;markDraft();}
function busy(on){running=on;$('input-fields').disabled=on;$('run').disabled=on;$('reset').disabled=on;[...$('presets').children].forEach(b=>b.disabled=on);text('run',on?'Running and verifying…':'Run simulation');$('controls').setAttribute('aria-busy',on);}
function errorMessage(data){if(Array.isArray(data.detail))return data.detail.map(e=>`${e.field}: ${e.message}`).join(' · ');return typeof data.detail==='string'?data.detail:'The simulation could not finish. Please retry.';}
async function execute(event){
  if(event)event.preventDefault();if(running||!$('controls').reportValidity())return;
  const submitted=readInputs();busy(true);$('error').hidden=true;text('draft-status','A fresh Nandatown run is in progress…');
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),22000);
  try{
    const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(submitted),signal:controller.signal});
    const result=await response.json();if(!response.ok)throw new Error(errorMessage(result));
    const previous=latest;latest=result;stage=5;renderResult(previous);text('connection','Simulation service connected');
  }catch(error){text('error',error.name==='AbortError'?'The service took too long to respond. Your last result has been kept. Retry when the service is available.':error.message);$('error').hidden=false;}
  finally{clearTimeout(timer);busy(false);markDraft();}
}
function renderResult(previous){
  const r=latest,m=r.metrics; $('results').hidden=false;text('verified','Run evidence verified');text('run-meta',`${r.run_id} · Fresh Nandatown simulation · ${m.message_count} messages`);
  text('committed',fmt(m.committed_kwh));text('settled',fmt(m.settled_kwh));text('released',fmt(m.released_payment));text('pending',fmt(m.pending_payment));
  const pending=r.result.trades.filter(t=>t.status==='pending'),cancelled=r.result.trades.filter(t=>t.status==='cancelled');
  let narrative;
  if(!r.result.trades.length)narrative='No trades were committed. Check whether prices overlap and whether reserves, budgets or the trading cap leave room for an offer.';
  else if(pending.length)narrative=`${fmt(m.settled_kwh)} of ${fmt(m.committed_kwh)} committed kWh settled. ${pending.length} trade(s) await valid meter data, so INR ${fmt(m.pending_payment)} remains reserved. Unresolved energy is not yet a confirmed delivery shortfall.`;
  else if(cancelled.length)narrative=`${fmt(m.settled_kwh)} of ${fmt(m.committed_kwh)} committed kWh settled. ${cancelled.length} trade(s) were cancelled under your missing-reading policy and their reservations released.`;
  else if(m.settled_kwh<m.committed_kwh-1e-8)narrative=`The agents committed ${fmt(m.committed_kwh)} kWh, but the meters supported ${fmt(m.settled_kwh)} kWh. The ${fmt(m.committed_kwh-m.settled_kwh)} kWh shortfall reduced payment; INR ${fmt(m.released_payment)} was released.`;
  else narrative=`All ${fmt(m.committed_kwh)} committed kWh were supported by the meter readings. Buyers paid INR ${fmt(m.seller_revenue)} to the sellers.`;
  text('outcome',narrative);
  const signed=n=>(n>=0?'+':'')+fmt(n);
  text('comparison',previous?`Compared with your previous run (${previous.run_id}): ${signed(m.committed_kwh-previous.metrics.committed_kwh)} kWh committed, ${signed(m.settled_kwh-previous.metrics.settled_kwh)} kWh settled.`:'Edit an input and run again to compare with this result.');
  $('trades').replaceChildren();r.result.trades.forEach(t=>tableRow($('trades'),[t.seller+' → '+t.buyer,fmt(t.price),fmt(t.committed_kwh),fmt(t.settled_kwh),fmt(t.settled_payment),t.status]));$('empty-trades').hidden=Boolean(r.result.trades.length);
  $('benefits').replaceChildren();m.buyers.forEach(b=>tableRow($('benefits'),[b.name,fmt(b.p2p_cost),fmt(b.grid_cost),fmt(b.savings),fmt(b.unfulfilled_p2p_kwh)]));
  text('tariff-note',`Grid costs use each buyer’s valid simulated import reading and an assumed INR ${fmt(r.inputs.grid_tariff)}/kWh tariff. Missing readings or pending trades make grid cost and savings unavailable. Savings exclude fees and taxes. Unfulfilled P2P demand refers to the expected demand, not an outage.`);
  text('seller-revenue','Seller revenue: '+m.sellers.map(s=>s.name+' INR '+fmt(s.revenue)).join(' · ')+'. Export opportunity cost is not modeled.');
  $('checks').replaceChildren();r.evidence.stages.forEach(s=>tableRow($('checks'),[s.name.replaceAll('_',' '),s.status.replaceAll('_',' ')]));text('run-inputs',JSON.stringify(r.inputs,null,2));
  urls.forEach(URL.revokeObjectURL);urls.length=0;
  function artifact(id,body,type){const url=URL.createObjectURL(new Blob([body],{type}));urls.push(url);$(id).href=url;}
  artifact('download-ledger',r.artifacts.ledger_csv,'text/csv');artifact('download-trace',JSON.stringify(r.events,null,2),'application/json');artifact('download-inputs',JSON.stringify(r.inputs,null,2),'application/json');const {artifacts,...publicResult}=r;artifact('download-result',JSON.stringify(publicResult,null,2),'application/json');artifact('download-report',r.artifacts.report_txt,'text/plain');artifact('native-replay',r.artifacts.native_html,'text/html');
  renderStage();
}
function renderStage(){
  if(!latest)return;const r=latest;[...$('steps').children].forEach((button,i)=>{button.classList.toggle('active',i===stage);button.setAttribute('aria-pressed',i===stage)});text('stage-title',`${stage+1}. ${stages[stage]}`);
  const copy=['Households observe their forecasts and validated preferences. The agents do not see the simulated meter outcomes you chose.','Sellers offer forecast surplus minus reserve. Buyers submit demand, price and spending limits. Nothing is committed yet.',`The utility applies an aggregate ${fmt(r.inputs.utility_cap_kwh)} kWh trading cap. This check does not model electrical network feasibility.`,'Buyer payment is reserved before the market records commitments. Promised energy is not yet measured delivery.','Only now does the meter role reveal the simulated import/export readings. Missing readings cannot support settlement.','Valid readings determine payment in commitment order. Completed trades release unused reservations. Missing-reading trades follow the policy you selected.'];text('stage-copy',copy[stage]);
  $('energy-bars').replaceChildren();const committed=stage>=3?r.result.committed_kwh:0,settled=stage>=5?r.result.settled_kwh:0,max=Math.max(r.result.committed_kwh,1);
  [['Committed',committed],['Settled',settled]].forEach(([label,value])=>{const row=document.createElement('div');row.className='bar-row';const name=document.createElement('span');name.textContent=label;const track=document.createElement('div');track.className='track';const meter=document.createElement('meter');meter.min=0;meter.max=max;meter.value=value;meter.setAttribute('aria-label',label+' kWh');track.append(meter);const amount=document.createElement('span');amount.textContent=fmt(value)+' kWh';row.append(name,track,amount);$('energy-bars').append(row)});
  let end=-1;r.events.forEach((e,i)=>{if(e.kind===eventKinds[stage])end=i;});const visible=r.events.slice(0,end+1).filter(e=>e.kind.startsWith('energy_')||e.kind==='message_sent').slice(-30);$('events').replaceChildren();visible.forEach(e=>{const item=document.createElement('details'),summary=document.createElement('summary'),detail=document.createElement('pre');summary.textContent=`${e.at.toFixed(2)} · ${e.observer} · ${e.kind.replace('energy_','')}`;detail.textContent=JSON.stringify(e.detail,null,2);item.append(summary,detail);$('events').append(item)});text('event-count',`${visible.length} recent entries at this stage. The full trace contains ${r.events.length} events.`);
}
$('controls').addEventListener('submit',execute);$('controls').addEventListener('input',markDraft);$('controls').addEventListener('change',markDraft);$('reset').onclick=()=>loadPreset(selected);
stages.forEach((name,i)=>{const b=document.createElement('button');b.type='button';b.textContent=name;b.onclick=()=>{stage=i;renderStage()};$('steps').append(b)});
async function init(){try{const response=await fetch('/api/presets');if(!response.ok)throw new Error('Unable to load the simulation inputs.');presets=(await response.json()).presets;Object.keys(titles).forEach(name=>{const b=document.createElement('button');b.type='button';b.dataset.name=name;b.textContent=titles[name];b.onclick=()=>loadPreset(name);$('presets').append(b)});loadPreset('normal');busy(false);text('connection','Simulation service connected');await execute();}catch(error){text('connection','Simulation service unavailable');text('error',error.message+' Refresh to reconnect.');$('error').hidden=false;}}
init();
