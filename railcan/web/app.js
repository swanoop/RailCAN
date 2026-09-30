"use strict";
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="railcan-token"]').content;
let schema = null, state = null, selectedId = null, noticeTimer = null, busy = false;
const faultLabels = {emergency_brake:"Emergency brake",door_obstruction:"Door obstruction",hvac_overheat:"HVAC overheating",low_voltage:"Low supply voltage",stuck_speed:"Freeze speed sensor",message_dropout:"Traction message dropout",checksum_error:"Corrupt checksum",counter_freeze:"Freeze alive counter",speed_spike:"Speed signal spike"};
const phases = {station:"Station dwell",accelerating:"Accelerating",cruising:"Cruising",braking:"Service braking",emergency:"Emergency braking"};
function node(tag, text, className) { const item = document.createElement(tag); if(text !== undefined) item.textContent = text; if(className) item.className = className; return item; }
function notify(message, error=false) { clearTimeout(noticeTimer); $("notice").textContent = message; $("notice").className = error ? "error" : ""; $("notice").hidden = false; noticeTimer=setTimeout(()=>$("notice").hidden=true,6000); }
async function api(route, body) {
  const response = await fetch(route,{method:body?"POST":"GET",headers:{"X-RailCAN-Token":token,...(body?{"Content-Type":"application/json"}:{})},...(body?{body:JSON.stringify(body)}:{})});
  const result=await response.json(); if(!response.ok) throw new Error(result.error||"Request failed"); return result;
}
function config(){return {scenario:$("scenario").value,duration:Number($("duration").value),seed:Number($("seed").value),rate:Number($("rate").value)};}
async function control(data){if(busy)return;busy=true;$("run").disabled=true;try{render(await api("/api/control",data));}catch(error){notify(error.message,true);}finally{busy=false;$("run").disabled=false;}}
function download(format){const anchor=node("a");anchor.href=`/api/export?format=${encodeURIComponent(format)}&token=${encodeURIComponent(token)}`;anchor.download=`railcan-capture.${format}`;document.body.append(anchor);anchor.click();anchor.remove();}
function clock(seconds, decimals=false){let mins=Math.floor(seconds/60),secs=seconds%60;return String(mins).padStart(2,"0")+":"+(decimals?secs.toFixed(2).padStart(5,"0"):String(Math.floor(secs)).padStart(2,"0"));}
function setMetric(id,value,unit,decimals){const el=$(id);el.replaceChildren(document.createTextNode(Number(value).toFixed(decimals)+" "),node("small",unit));}
function render(next){
  state=next;const t=next.telemetry;
  $("status").textContent=next.status.toUpperCase();$("status").className="status-badge "+next.status;
  setMetric("speed",t.speed_kph,"km/h",1);setMetric("distance",t.distance_m/1000,"km",2);setMetric("load",next.nominal_bus_load_percent,"%",2);
  $("frames").textContent=next.frame_count.toLocaleString();$("mode").textContent=phases[t.phase]||t.mode_label;$("station").textContent="Station "+String(t.station_index+1).padStart(2,"0");
  $("fps").textContent=`${next.nominal_fps.toFixed(0)} frames / simulated second`;
  $("traction").textContent=t.traction_pct.toFixed(0)+"%";$("traction-bar").value=t.traction_pct;
  $("braking").textContent=t.brake_pct.toFixed(0)+"%";$("brake-bar").value=t.brake_pct;
  $("doors").textContent=t.doors_locked?"Locked":"Released";$("doors").className=t.door_fault?"danger":"";
  $("pressure").textContent=t.brake_pipe_bar.toFixed(2)+" bar";$("voltage").textContent=t.supply_voltage.toFixed(0)+" V";
  $("temperature").textContent=t.cabin_c.toFixed(1)+" °C";$("checksum-errors").textContent=next.checksum_errors.toLocaleString();$("checksum-errors").className=next.checksum_errors?"danger":"";
  const time=next.status==="complete"?next.config.duration:next.time_s;
  $("clock").textContent=clock(time,true)+" / "+clock(next.config.duration);$("progress").max=next.config.duration;$("progress").value=time;
  $("fault-status").textContent=next.active_faults.length?next.active_faults.map(f=>faultLabels[f]||f).join(" · "):"No active faults";$("fault-status").className=next.active_faults.length?"active":"";
  $("run").replaceChildren(document.createTextNode(next.status==="running"?"Pause simulation":next.status==="paused"?"Resume simulation":"Start simulation"),node("span",next.status==="running"?"Ⅱ":"▶"));
  $("inject").disabled=!["running","paused"].includes(next.status);
  $("train-scene").classList.toggle("moving",next.status==="running"&&t.speed_kph>0);$("train-scene").classList.toggle("doors-open",Boolean(t.left_doors||t.right_doors));
  $("journey-label").textContent=t.phase==="station"?"Platform dwell":phases[t.phase]||t.mode_label;
  $("journey-detail").textContent=t.emergency?"Emergency brake applied · traction inhibited":t.door_obstruction?"Door obstruction · departure held":!t.doors_locked?"Passenger doors open · traction inhibited":"Doors secured · vehicle systems online";
  renderMessages();renderRaw();renderEvents();drawChart();
}
function renderMessages(){
  const latest=new Map(state.latest.map(f=>[f.can_id,f]));const fragment=document.createDocumentFragment();
  for(const message of schema.profile.messages){
    const frame=latest.get(message.can_id),row=node("tr");row.tabIndex=0;row.setAttribute("aria-label",`Decode ${message.name}`);row.classList.toggle("selected",selectedId===message.can_id);
    const identifier=message.can_id.toString(16).toUpperCase().padStart(message.extended?8:3,"0");
    row.append(node("td","0x"+identifier),node("td",message.name),node("td",message.period_ms+" ms"));
    row.append(node("td",frame?frame.data_hex.match(/.{1,2}/g).join(" "):"—","payload"),node("td",frame?String(frame.signals.AliveCounter):"—","counter"));
    let text="WAITING",kind="waiting";
    if(frame){const stale=state.time_s-frame.timestamp>message.period_ms/1000*2.1;text=stale?"STALE":frame.checksum_valid?"VALID":"BAD CHECKSUM";kind=stale?"stale":frame.checksum_valid?"":"bad";}
    const cell=node("td");cell.append(node("span",text,"integrity "+kind));row.append(cell);
    const choose=()=>{selectedId=message.can_id;renderMessages();};row.addEventListener("click",choose);row.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();choose();}});fragment.append(row);
  }
  $("messages").replaceChildren(fragment);renderDecode();
}
function renderDecode(){
  $("decode-panel").hidden=selectedId===null;if(selectedId===null)return;
  const message=schema.profile.messages.find(m=>m.can_id===selectedId),frame=state.latest.find(f=>f.can_id===selectedId);
  $("decode-title").textContent=message.name+" · decoded latest frame";
  const fragment=document.createDocumentFragment();
  for(const signal of [...message.signals,{name:"AliveCounter",unit:""},{name:"Checksum",unit:""}]){
    const tile=node("div",undefined,"signal");tile.append(node("span",signal.name,"signal-label"));let value=frame?frame.signals[signal.name]:"—";
    if(typeof value==="number")value=Number(value.toFixed(3)).toString();tile.append(node("span",value+(signal.unit?" "+signal.unit:""),"signal-value"));fragment.append(tile);
  }
  $("signals").replaceChildren(fragment);
}
function renderRaw(){
  const filter=$("filter").value.toLowerCase();const fragment=document.createDocumentFragment();
  const frames=state.frames.filter(f=>(f.can_id_hex+" "+f.message).toLowerCase().includes(filter));
  for(const frame of frames){const row=node("div",undefined,"raw-row"+(frame.checksum_valid===false?" bad":""));row.append(node("span",frame.timestamp.toFixed(3)),node("span",frame.can_id_hex),node("span",frame.data_hex.match(/.{1,2}/g).join(" ")),node("span",frame.message));fragment.append(row);}
  if(!frames.length)fragment.append(node("div",filter?"No matching frames.":"Waiting for CAN traffic.","empty-log"));$("raw-frames").replaceChildren(fragment);
}
function renderEvents(){const fragment=document.createDocumentFragment();for(const event of [...state.events].reverse()){const el=node("div",undefined,"event");el.append(node("span",clock(event.time_s,true),"event-time"),document.createTextNode(event.text));fragment.append(el);}$("events").replaceChildren(fragment);}
function drawChart(){
  if(!state)return;const canvas=$("speed-chart"),rect=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1;
  const width=rect.width,height=rect.height;if(width<1||height<1)return;canvas.width=Math.round(width*ratio);canvas.height=Math.round(height*ratio);
  const ctx=canvas.getContext("2d");ctx.scale(ratio,ratio);ctx.clearRect(0,0,width,height);
  const points=state.history,left=40,right=12,top=12,bottom=29,plotW=width-left-right,plotH=height-top-bottom;
  const max=Math.max(schema.profile.train.target_speed_kph*1.2,25,...points.map(p=>Math.max(p.speed_kph,p.reported_speed_kph||0)));
  const ymax=Math.ceil(max/25)*25;const end=Math.max(10,state.time_s),start=Math.max(0,end-72);
  ctx.font="9px ui-monospace, Consolas, monospace";ctx.textBaseline="middle";
  for(let i=0;i<=4;i++){let y=top+plotH*i/4;ctx.strokeStyle="#26363f";ctx.lineWidth=1;ctx.setLineDash([]);ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(width-right,y);ctx.stroke();ctx.fillStyle="#77909f";ctx.textAlign="right";ctx.fillText(String(Math.round(ymax*(1-i/4))),left-9,y);}
  for(let i=0;i<=4;i++){ctx.textAlign=i===0?"left":i===4?"right":"center";ctx.fillStyle="#77909f";ctx.fillText((start+(end-start)*i/4).toFixed(0)+"s",left+plotW*i/4,height-12);}
  const x=t=>left+(t-start)/(end-start)*plotW,y=v=>top+plotH*(1-v/ymax);
  if(points.length>1){ctx.beginPath();ctx.moveTo(x(points[0].time_s),top+plotH);for(const point of points)ctx.lineTo(x(point.time_s),y(point.speed_kph));ctx.lineTo(x(points[points.length-1].time_s),top+plotH);ctx.closePath();const gradient=ctx.createLinearGradient(0,top,0,top+plotH);gradient.addColorStop(0,"#5ce0b42b");gradient.addColorStop(1,"#5ce0b400");ctx.fillStyle=gradient;ctx.fill();}
  for(const [key,color,dashes] of [["speed_kph","#dce8ef",[]],["reported_speed_kph","#5ce0b4",[5,4]]]){ctx.beginPath();let moved=false;for(const point of points){if(point[key]===null||point.time_s<start)continue;if(!moved){ctx.moveTo(x(point.time_s),y(point[key]));moved=true;}else ctx.lineTo(x(point.time_s),y(point[key]));}ctx.strokeStyle=color;ctx.lineWidth=1.7;ctx.setLineDash(dashes);ctx.stroke();}
  $("chart-empty").hidden=state.frame_count>0;canvas.setAttribute("aria-label",`Vehicle speed ${state.telemetry.speed_kph.toFixed(1)} km/h. ${points.length} samples over the last 72 simulated seconds.`);
}
async function poll(){try{render(await api("/api/state"));}catch(error){$("status").textContent="DISCONNECTED";$("status").className="status-badge error";}setTimeout(poll,250);}
async function boot(){
  try{schema=await api("/api/schema");for(const [name,scenario] of Object.entries(schema.scenarios)){const option=node("option",scenario.label);option.value=name;$("scenario").append(option);}
    for(const kind of schema.faults){const option=node("option",faultLabels[kind]||kind);option.value=kind;$("fault").append(option);}
    $("version").textContent="v"+schema.version;$("footer-version").textContent=schema.version;$("profile-name").textContent=schema.profile.name;
    $("profile-meta").replaceChildren(document.createTextNode(`${schema.profile.messages.length} messages · Classical CAN`),node("br"),document.createTextNode(`${schema.profile.bitrate/1000} kbit/s · synthetic layout`));
    $("message-count").textContent=schema.profile.messages.length+" MESSAGE DEFINITIONS";
    const description=()=>$("scenario-description").textContent=schema.scenarios[$("scenario").value].description;description();$("scenario").addEventListener("change",description);
    $("setup").addEventListener("submit",event=>{event.preventDefault();const action=state.status==="running"?"pause":state.status==="paused"?"resume":"start";control({action,...(action==="start"?{config:config()}:{})});});
    $("reset").addEventListener("click",()=>control({action:"reset",config:config()}));
    $("rate").addEventListener("change",()=>{if(state&&["running","paused"].includes(state.status))control({action:"rate",rate:Number($("rate").value)});});
    $("inject").addEventListener("click",()=>control({action:"fault",kind:$("fault").value,duration:Number($("fault-duration").value)}));
    $("clear-faults").addEventListener("click",()=>control({action:"clear_faults"}));
    $("close-decode").addEventListener("click",()=>{selectedId=null;renderMessages();});$("filter").addEventListener("input",()=>state&&renderRaw());
    document.querySelectorAll("[data-export]").forEach(button=>button.addEventListener("click",()=>download(button.dataset.export)));
    $("download-dbc").addEventListener("click",()=>download("dbc"));$("download-profile").addEventListener("click",()=>download("profile"));
    new ResizeObserver(drawChart).observe($("speed-chart"));await poll();
  }catch(error){notify("Unable to open the dashboard: "+error.message,true);}
}
boot();
