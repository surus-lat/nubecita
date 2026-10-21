/* Portal ADMIN de nubecita — bundle diferido: se carga con <script> inyectado
function VMNET(){const p=((typeof brand!=="undefined"&&brand&&brand.vm_net)||"10.20.1").split(".");return p[0]+"."+p[1];}
   solo cuando session.role==='admin' (ver loadAdminBundle en index.html).
   Script clásico: comparte el scope global con el inline de index.html
   (api, $, esc, pill, bar, toast, busy, askConfirm, fmtCr, adminTab, etc.). */
/* ══════════ ADMIN ══════════ */
function adminHtml(){
  if(adminTab==="overview")return `
    <div id="aOverview">
      <div id="ovInc"></div>
      <div class="statrow" id="ovStats"></div>
      <div class="card mt"><h2>Capacidad — cuota vs. uso (corriendo)</h2><div id="ovCap" data-err="ovCap"></div></div>
      <div class="grid mt">
        <div class="card"><h2>Nodo Proxmox</h2><div id="ovNode" data-err="ovNode"></div></div>
        <div class="card"><h2>Plano de control</h2><div id="ovCtrl" data-err="ovCtrl"></div></div>
      </div>
      <div class="card mt"><h2>Consumo por cliente (últimos 30 días)</h2><div id="ovUsage" data-err="ovUsage"></div></div>
      <div class="card mt"><h2>Costo por hora (tarifas)</h2><div id="ovRates" data-err="ovRates"></div></div>
      <div class="card mt"><h2>SSH gateways (jump por VM)</h2><div id="ovGw" data-err="ovGw"></div></div>
    </div>`;
  if(adminTab==="resources")return `
    <div id="aResources">
      <div class="card"><h2>Todos los recursos (ledger + estado real)</h2>
        <div class="hint" style="margin:0 0 10px">«Desfasado» = el ledger dice un estado y PVE/Nomad otro. Borrar forzoso limpia el ledger aunque el backend esté roto.</div>
        <div class="filt" id="resFilt"></div>
        <div id="resList" data-err="resList"></div>
      </div>
    </div>`;
  if(adminTab==="tickets")return `
    <div id="aTickets">
      <div class="card">
        <div style="display:flex;align-items:center;gap:10px">
          <h2 style="margin:0;flex:1">Tickets / reportes de usuarios</h2>
          <span class="muted" style="text-transform:none;font-weight:400">clic en una fila abre el detalle</span>
        </div>
        <div class="filt mt" id="tkFilt"></div>
        <div id="tkList" data-err="tkList"></div>
      </div>
    </div>`;
  return `
    <div id="aUsers">
      <div class="card">
        <div style="display:flex;align-items:center;gap:10px">
          <h2 style="margin:0;flex:1">Usuarios / Clientes <span class="muted" style="text-transform:none;font-weight:400">(cada usuario es un cliente — clic en una fila abre el editor)</span></h2>
          <button onclick="openUserModal()" style="white-space:nowrap">+ Nuevo</button>
        </div>
        <div id="uList" class="mt" data-err="uList"></div>
      </div>
    </div>`;
}

async function renderAdmin(){
  const a=$("#app");
  if(a.dataset.tab!==adminTab){a.dataset.tab=adminTab;a.innerHTML=adminHtml();}
  if(adminTab==="overview")await renderOverview();
  else if(adminTab==="resources")await renderResources();
  else if(adminTab==="tickets")await renderTickets();
  else await renderUsers();
}
function adminRender(){
  const a=$("#app"); if(a.dataset.role!=="admin")return;
  if(a.dataset.tab!==adminTab){a.dataset.tab=adminTab;a.innerHTML=adminHtml();}
  if(adminTab==="overview")renderOverview();
  else if(adminTab==="resources")renderResources();
  else if(adminTab==="tickets")renderTickets();
  else renderUsers();
}

async function renderOverview(){
  const [ov,sys,rt,allRes]=await Promise.all([
    api("/admin/overview"),
    cachedApi("/admin/system",30000).catch(()=>({})),
    cachedApi("/admin/rates",30000).catch(()=>null),
    api("/admin/resources").catch(()=>[])
  ]);
  drawIncidentBand(ov,sys,allRes);
  const t=ov.total_used,q=ov.total_quota;
  const gb=x=>Math.round(x/1024);
  const stat=(k,v,sub="")=>`<div class="stat"><span class="k">${k}</span><span class="v">${v}${sub?` <small>${sub}</small>`:""}</span></div>`;
  $("#ovStats").innerHTML=
    stat("Clientes",ov.counts.clients)+
    stat("VMs corriendo",ov.counts.running_vms)+
    stat("Contenedores",ov.counts.running_containers)+
    stat("CPU en uso",`${t.cpu}<small> / ${q.cpu} cores</small>`)+
    stat("RAM en uso",`${gb(t.ram_mb)}<small> / ${gb(q.ram_mb)} GB</small>`)+
    stat("Disco en uso",`${t.disk_gb}<small> / ${q.disk_gb} GB</small>`);
  const rows=ov.clients.map(c=>{
    const u=ov.per_client[c.id]||{vms:0,containers:0,cpu:0,ram_mb:0,disk_gb:0};
    return `<tr><td><b>${esc(c.id)}</b></td>
      <td class="mono" style="font-size:12px">${c.subnet?`${VMNET()}.${c.subnet}.0/24`:"—"}</td>
      <td style="min-width:110px">${qval(u.cpu,c.cpu_quota)}</td>
      <td style="min-width:110px">${qval(gb(u.ram_mb),gb(c.ram_quota_mb)," GB")}</td>
      <td style="min-width:110px">${qval(u.disk_gb,c.disk_quota_gb," GB")}</td>
      <td>${qval(u.vms,c.max_vms)}</td><td>${qval(u.containers,c.max_containers)}</td></tr>`;}).join("");
  $("#ovCap").innerHTML=ov.clients.length
    ?`<div class="tblwrap"><table><tr><th>Cliente</th><th>LAN</th><th>CPU (uso/cuota)</th><th>RAM</th><th>Disco</th><th>VMs</th><th>Servicios</th></tr>${rows}</table></div>`
    :`<div class="empty">Sin clientes.</div>`;
  const n=sys.pve_node||{};
  $("#ovNode").innerHTML=n.error
    ?`<div class="msg err">PVE no responde: ${esc(n.error)}</div>`
    :`<div class="qrow"><span>Estado</span>${pill(n.status||"unknown")}</div>
      <div class="qrow"><span>CPU</span><span class="val">${n.cpu??"—"}%</span></div>${bar(n.cpu||0,100)}
      <div class="qrow"><span>RAM</span><span class="val">${Math.round((n.memory_used||0)/1024)} / ${Math.round((n.memory_total||0)/1024)} GB</span></div>
      ${bar(n.memory_used||0,n.memory_total||1)}
      <div class="qrow"><span>Disco</span><span class="val">${Math.round((n.disk_used||0)/1048576)} / ${Math.round((n.disk_total||0)/1048576)} GB</span></div>
      ${bar(n.disk_used||0,n.disk_total||1)}`;
  const m=sys.nomad||{};
  $("#ovCtrl").innerHTML=
    `<div class="qrow"><span>Nomad (contenedores)</span>${m.status==="up"?pill("running"):pill(m.status||"stopped")}</div>
     ${m.version?`<div class="hint">v${esc(m.version)}${m.leader?" · leader "+esc(m.leader):""}</div>`:
       (m.error?`<div class="msg err">${esc(m.error)}</div>`:"")}
     <div class="qrow" style="margin-top:14px"><span>Backend cloudapi</span>${pill("running")}</div>
     <div class="hint">Uvicorn en el CT 6003, systemd. Si la página carga, el backend está vivo.</div>`;
  const gws=Array.isArray(sys.ssh_gateways)?sys.ssh_gateways:[];
  $("#ovGw").innerHTML=gws.length
    ?`<div class="tblwrap"><table><tr><th>Unit</th><th>Estado</th></tr>${gws.map(g=>
       `<tr><td class="mono" style="font-size:12px">${esc(g.unit.replace(".service",""))}</td>
        <td>${(g.active==="active"&&g.sub==="running")?pill("running"):pill("stopped")}</td></tr>`).join("")}</table></div>`
    :((sys.ssh_gateways&&sys.ssh_gateways.error)?`<div class="msg err">${esc(sys.ssh_gateways.error)}</div>`
     :`<div class="empty">Sin gateways SSH activos.</div>`);
  renderRates(rt);
  renderAdminUsage();
}
/* Banda de incidentes: estado global + drift + provisioning atascado */
function drawIncidentBand(ov,sys,rows){
  const host=$("#ovInc"); if(!host)return;
  const list=Array.isArray(rows)?rows:[];
  const driftN=list.filter(r=>r.drift).length;
  const provStuck=list.filter(r=>r.status==="provisioning"
    &&r.created_at&&(Date.now()-new Date(r.created_at).getTime()>10*60*1000)).length;
  const pveStatus=(sys.pve_node&&sys.pve_node.status)||"desconocido";
  const nomadStatus=(sys.nomad&&sys.nomad.status==="up")?"up":((sys.nomad&&sys.nomad.status)||"desconocido");
  const seg=`<span class="sep">·</span>`;
  const base=`${ov.counts.clients} clientes${seg}${ov.counts.running_vms} VMs running${seg}${ov.counts.running_containers} contenedores${seg}${driftN} desfasados${seg}PVE: ${esc(pveStatus)}${seg}Nomad: ${esc(nomadStatus)}`;
  const driftMsg=driftN>0?`<span class="driftwarn">${driftN} recurso(s) desfasados — revisá Recursos</span>`:"";
  const provMsg=provStuck>0?`<span class="provwarn">⏳ ${provStuck} VM(s) en «provisioning» hace +10 min</span>`:"";
  const bad=driftN>0||provStuck>0;
  host.innerHTML=`<div class="incband ${bad?"warn":"ok"}">${base}${bad
    ?`<span class="sep">·</span>${driftMsg}${provMsg?`<span class="sep">·</span>${provMsg}`:""}`
    :'<span class="sep">·</span>Sin incidentes activos'}</div>`;
}
// /admin/usage cacheado 5 min: el auto-refresh de 15s no lo refetchea cada tick
let admUsageCache=null, admUsageAt=0;
async function renderAdminUsage(){
  const host=$("#ovUsage"); if(!host)return;
  const now=Date.now();
  if(!admUsageCache||now-admUsageAt>=300000){
    try{admUsageCache=await api("/admin/usage?days=30","GET",null,"admin");admUsageAt=now;}
    catch(e){if(!admUsageCache){host.innerHTML=`<div class="msg err">${esc(e.message)}</div>`;return;}}
  }
  const d=admUsageCache, rows=d.by_user||[];
  host.innerHTML=rows.length
    ?`<div class="hint" style="margin-top:0">Suma neta de créditos (top-ups y billing) por cliente — se actualiza cada 5 min. Negativo = consumo.</div>
      <div class="tblwrap"><table><tr><th>Cliente</th><th>Delta (cr)</th><th>Eventos</th></tr>
      ${rows.map(r=>`<tr><td><b>${esc(r.username)}</b></td>
        <td class="mono" style="font-size:12px;color:${r.delta_net<0?"var(--bad)":"var(--ok)"}">${r.delta_net<0?"":"+"}${fmtCr(r.delta_net)}</td>
        <td class="muted" style="font-size:12px">${r.events_n}</td></tr>`).join("")}
      </table></div>`
    :`<div class="empty">Sin movimientos en los últimos 30 días.</div>`;
}
const RATE_META=[["cpu","vCPU (cr/core/h)"],["ram_gb","RAM (cr/GB/h)"],["disk_gb","Disco (cr/GB/h)"],["gpu","GPU (cr/unidad/h)"]];
function renderRates(rt){
  const host=$("#ovRates"); if(!host||!rt)return;
  // no pisar el input mientras el admin está editando un valor
  const ae=document.activeElement;
  if(ae && host.contains(ae) && ae.tagName==="INPUT")return;
  const r=rt.rates,df=rt.defaults;
  const pool=8*(r.cpu||0)+32*(r.ram_gb||0)+40*(r.disk_gb||0)+(r.gpu||0);
  host.innerHTML=`
    <div class="hint" style="margin-top:0">La VM del pool completo (8c / 32 GB / 40 GB + 1 GPU) cuesta <b>${(Math.round(pool*100)/100).toLocaleString("es-AR")}</b> cr/h → un top-up de 213 créditos dura ~${pool>0?Math.round(213/pool*10)/10:0} h. El biller aplica la nueva tasa desde el próximo tick (≤60 s).</div>
    <div class="row" style="margin-top:10px">
      ${RATE_META.map(([k,lbl])=>`<div style="min-width:130px"><label>${lbl}</label>
        <input id="rate_${k}" type="number" min="0" step="any" value="${r[k]??""}"></div>`).join("")}
      <button onclick="saveRates(this)">Guardar</button>
    </div>
    <div class="row" style="margin-top:6px"><button class="ghost" onclick="resetRates(this)" title="Volver a los valores iniciales">↺ Valores por defecto</button>
      <span class="hint" style="margin:0">por defecto: cpu ${df.cpu} · ram ${df.ram_gb} · disco ${df.disk_gb} · gpu ${df.gpu}</span></div>`;
}
async function saveRates(btn){
  const body={};
  let bad=null;
  for(const [k] of RATE_META){
    const v=parseFloat($("#rate_"+k).value);
    if(isNaN(v)){bad=k;break;}
    body[k]=v;
  }
  if(bad){errT("revisá el campo "+bad);return;}
  busy(btn,"Guardando…");
  try{await api("/admin/rates","PUT",body,null,"admin");okT("tarifas actualizadas");cacheBust("/admin/rates");}
  catch(e){errT(e.message);}
  finally{unbusy(btn);}
}
async function resetRates(btn){
  busy(btn,"Cargando…");
  try{
    const d=await api("/admin/rates","GET",null,"admin");
    for(const [k] of RATE_META){const el=$("#rate_"+k);if(el)el.value=d.defaults[k];}
  }catch(e){errT(e.message);}
  finally{unbusy(btn);}
}

let adminResCache=[],adminResFilt="all";
function adminResMatch(r){
  if(adminResFilt==="all")return true;
  if(adminResFilt==="drift")return !!r.drift;
  if(adminResFilt==="running")return r.status==="running"||r.pve_status==="running";
  if(adminResFilt==="stopped")return r.status==="stopped";
  if(adminResFilt==="deleted")return r.status==="deleted";
  return true;
}
function drawResFilt(){
  const el=$("#resFilt"); if(!el)return;
  const opts=[["all","Todas"],["drift","Drift"],["running","Running"],["stopped","Stopped"],["deleted","Deleted"]];
  el.innerHTML=opts.map(([k,l])=>`<button class="${k===adminResFilt?"on":""}" onclick="setResFilter('${k}')">${l}</button>`).join("");
}
function setResFilter(v){adminResFilt=v;drawResFilt();drawResList(adminResCache);}
function drawResList(rows){
  const vis=rows.filter(adminResMatch);
  $("#resList").innerHTML=vis.length
    ?`<div class="tblwrap"><table><tr><th>Cliente</th><th>Tipo</th><th>Ref</th><th>Nombre</th><th>SSH</th><th>Spec</th><th>Fecha</th><th>Estado ledger</th><th>Real</th><th></th></tr>
     ${vis.map(r=>`<tr>
       <td>${esc(r.client||"—")}</td>
       <td>${r.kind==="vm"?"VM":"servicio"}</td>
       <td class="mono" style="font-size:12px">${esc(r.ref_id)}</td>
       <td>${esc(r.name||"—")}</td>
       <td class="mono" style="font-size:12px">${r.host_id?esc(r.host_id+"@"+sshDom()):"—"}</td>
       <td style="font-size:12px" class="muted">${r.cpu?r.cpu+"c":""} ${r.ram_mb?Math.round(r.ram_mb/1024)+"G":""} ${r.disk_gb?r.disk_gb+"G":""}</td>
       <td class="muted" style="font-size:12px"><nc-time ts="${esc(String(r.created_at||""))}"></nc-time></td>
       <td>${pill(r.status)}${r.drift?'<span class="pill drift">desfasado</span>':""}</td>
       <td>${r.kind==="vm"&&r.status!=="deleted"?pill(r.pve_status||"unknown"):(r.kind==="vm"?"—":"nomad")}</td>
       <td style="white-space:nowrap;text-align:right">${r.status!=="deleted"
         ?(r.kind==="vm"
           ?`<button class="ghost sm" onclick="vmAction('${r.ref_id}','start',this)" title="Iniciar">▶</button>
             <button class="ghost sm" onclick="vmAction('${r.ref_id}','stop',this)" title="Detener">⏸</button>
             <button class="ghost sm" onclick="vmAction('${r.ref_id}','restart',this)" title="Reiniciar">↻</button>`:"")
           +`<button class="danger sm" onclick="forceDel('${r.kind}','${esc(r.ref_id)}','${esc(r.name||r.ref_id)}',this)" title="Borrar forzoso">✕</button>`
         :""}</td></tr>`).join("")}
     </table></div>`
    :`<div class="empty">Sin recursos en el ledger.</div>`;
}
async function renderResources(){
  const rows=await api("/admin/resources");
  adminResCache=rows; drawResFilt(); drawResList(rows);
}
async function vmAction(vmid,action,btn){
  const label={start:"iniciando",stop:"deteniendo",restart:"reiniciando"}[action];
  toast(`VM ${vmid}: ${label}… (puede tardar unos segundos)`,"info");
  if(btn)busy(btn);
  try{const r=await api("/admin/vm/"+vmid+"/"+action,"POST");
    okT(`VM ${vmid} → ${r.status}`);}
  catch(e){errT(e.message);}
  if(btn)unbusy(btn);
  setTimeout(renderResources,600);
}
async function forceDel(kind,ref,name,btn){
  if(!await askConfirm(`Borrar forzosamente «${name}»`,`${kind} ${ref}. Se intenta parar/borrar en PVE/Nomad y luego se limpia el ledger.`))return;
  if(btn)busy(btn,"Borrando…");
  try{await api(`/admin/resource/${kind}/${encodeURIComponent(ref)}`,"DELETE");okT(`${name} eliminado`);}
  catch(e){errT(e.message);}
  if(btn)unbusy(btn);
  setTimeout(renderResources,600);
}

async function renderUsers(){
  const list=await api("/admin/users?full=true","GET",null,"admin");
  usersCache=list;
  // full=true trae ya usados/créditos/eventos en la misma respuesta (sin N+1)
  const html=list.map(u=>{
    const used=u.used;
    const uk=used?used.vm_cpu+used.c_cpu:0, ur=used?Math.round((used.vm_ram+used.c_ram)/1024):0,
          ud=used?used.vm_disk+used.c_disk:0, vn=used?used.vms:0, cn=used?used.containers:0;
    return `<tr style="cursor:pointer" onclick="selectUser('${esc(u.username)}')">
      <td><b>${esc(u.username)}</b><div class="muted" style="font-size:11px">desde ${fdate(u.created_at)}</div></td>
      <td>${u.role==="admin"?`<span class="pill other">admin</span>`:`<span class="pill pending">client</span>`}</td>
      <td class="mono" style="font-size:12px">${u.subnet?`${VMNET()}.${u.subnet}.0/24`:"—"}</td>
      <td>${u.role==="client"?qval(uk,u.cpu_quota):"—"}</td>
      <td style="min-width:110px">${u.role==="client"?`${qval(ur,Math.round(u.ram_quota_mb/1024)," GB")}${bar(ur,u.ram_quota_mb/1024)}`:"—"}</td>
      <td style="min-width:110px">${u.role==="client"?`${qval(ud,u.disk_quota_gb," GB")}${bar(ud,u.disk_quota_gb)}`:"—"}</td>
      <td>${u.role==="client"?`${qval(vn,u.max_vms)} · ${qval(cn,u.max_containers)}`:"—"}</td>
      <td class="mono" style="font-size:12px;text-align:right">${u.role==="client"&&u.credits!=null?fmtCr(u.credits):"—"}</td>
      <td style="text-align:right">${u.role==="admin"
        ?`<button class="ghost sm" onclick="setUserPass('${esc(u.username)}',this)" title="Cambiar contraseña">🔑</button>
          <button class="danger sm" onclick="delUser('${esc(u.username)}',this)" title="Quitar">✕</button>`
        :`<button class="ghost sm" onclick="regenToken(this,'${esc(u.username)}')" title="Regenerar token API">🔁</button>
          <button class="ghost sm" onclick="setUserPass('${esc(u.username)}',this)" title="Cambiar contraseña">🔑</button>
          <button class="danger sm" onclick="delUser('${esc(u.username)}',this)" title="Borrar">✕</button>`}</td>
    </tr>`;}).join("");
  $("#uList").innerHTML=list.length
    ?`<div class="tblwrap"><table><tr><th>Usuario</th><th>Rol</th><th>LAN</th><th>CPU</th><th>RAM</th><th>Disco</th><th>VMs · Svc</th><th>Créditos</th><th></th></tr>${html}</table></div>`
    :`<div class="empty">Sin usuarios.</div>`;
  if(!selectedUser||!list.some(u=>u.username===selectedUser))selectedUser=list.length?list[0].username:null;
  fillEditor();
}
let usersCache=[],selectedUser=null;
function selectUser(id){selectedUser=id;fillEditor();const dr=$("#admDrawer");if(dr)dr.classList.add("open");
  const tt=$("#edTitle");if(tt)tt.textContent="Editar «"+id+"»";}
function fillEditor(){
  const body=$("#edBody");if(!body)return;
  const u=usersCache.find(x=>x.username===selectedUser);
  if(!u){body.innerHTML=`<div class="empty">Seleccioná un usuario arriba para editarlo.</div>`;return;}
  if(u.role==="admin"){
    body.innerHTML=`<div class="qrow" style="margin-top:0"><span><b>${esc(u.username)}</b></span>
      <span class="pill other" style="margin-left:8px">admin</span></div>
      <div class="hint" style="margin-top:10px">Admin: solo puede cambiar contraseña.</div>`;
    return;
  }
  body.innerHTML=`
    <div class="qrow" style="margin-top:0"><span><b>${esc(u.username)}</b></span>
      <span class="mono muted" style="font-size:12px">${{u.subnet?"LAN "+((brand&&brand.vm_net)||"10.20.1").split(".").slice(0,2).join(".")+"."+u.subnet+".0/24":"sin LAN"}}</span></div>
    <div class="row">
      <div><label>CPU (cores)</label><input id="eCpu" type="number" value="${u.cpu_quota}"></div>
      <div><label>RAM (GB)</label><input id="eRam" type="number" value="${Math.round(u.ram_quota_mb/1024)}"></div>
      <div><label>Disco (GB)</label><input id="eDisk" type="number" value="${u.disk_quota_gb}"></div>
    </div>
    <div class="row">
      <div><label>Max VMs</label><input id="eVms" type="number" value="${u.max_vms}"></div>
      <div><label>Max servicios</label><input id="eSvs" type="number" value="${u.max_containers}"></div>
    </div>
    <div class="row" style="margin-top:14px">
      <button onclick="saveQuota(this)">Guardar cuotas</button>
    </div>
    <div class="mt" style="border-top:1px solid var(--line)"></div>
    <div class="qrow" style="margin-top:14px"><span><b>Créditos</b></span>
      <span id="crBal" class="mono" style="font-size:13px">cargando…</span></div>
    <div class="row" style="margin-top:4px">
      <div style="max-width:200px"><label>Setear saldo</label><input id="eCredits" type="number" min="0" step="1" placeholder="nuevo saldo"></div>
    </div>
    <div class="hint">Setear escribe el saldo exacto (ej. 213 = ~20 h con la VM del pool completo corriendo). Un top-up suma al saldo actual.</div>
    <div class="row" style="margin-top:10px">
      <button class="ghost" onclick="setCredits(this)">Setear saldo</button>
    </div>
    <div id="crEvents" class="mt"></div>
    <div class="mt" style="border-top:1px solid var(--line);padding-top:14px">
      <h3 style="font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:0 0 8px">Tokens API</h3>
      <div class="hint" style="margin-top:0">Revocación individual: el resto de tokens del usuario sigue funcionando. «Regenerar token» (🔁 arriba) revoca todos.</div>
      <div id="admTok" class="mt"></div>
    </div>`;
  loadCredits(u.username);
  loadAdmTokens(u.username);
}
async function loadAdmTokens(username){
  const host=$("#admTok"); if(!host)return;
  let d; try{d=await api("/admin/users/"+encodeURIComponent(username)+"/tokens","GET",null,"admin");}
  catch(e){host.innerHTML=`<div class="msg err">${esc(e.message)}</div>`;return;}
  host.innerHTML=d.tokens.length
    ?`<div class="tblwrap"><table><tr><th>Token</th><th>Creado</th><th>Último uso</th><th></th></tr>
      ${d.tokens.map(t=>`<tr>
        <td class="mono" style="font-size:12px">${esc(t.label||"sin etiqueta")}</td>
        <td class="muted" style="font-size:12px">${fdate(t.created_at)}</td>
        <td class="muted" style="font-size:12px">${t.last_used_at?String(t.last_used_at).slice(0,16).replace("T"," ")+" UTC":"nunca"}</td>
        <td style="text-align:right"><button class="danger sm" onclick="admRevokeToken('${esc(username)}','${esc(t.id)}','${esc(t.label||"sin etiqueta")}',this)" title="Revocar este token">✕</button></td>
      </tr>`).join("")}
      </table></div>`
    :`<div class="empty" style="padding:6px">Sin tokens.</div>`;
}
async function admRevokeToken(username,id,label,btn){
  if(!await askConfirm(`Revocar token «${label}» de ${username}`,"Cualquier uso con este token deja de funcionar de inmediato. Esta acción no se puede deshacer."))return;
  if(btn)busy(btn,"Revocando…");
  try{await api("/admin/users/"+encodeURIComponent(username)+"/tokens/"+encodeURIComponent(id),"DELETE",null,"admin");okT(`token de ${username} revocado`);loadAdmTokens(username);}
  catch(e){errT(e.message);}
  finally{if(btn)unbusy(btn);}
}
async function loadCredits(username){
  const host=$("#crBal"),ev=$("#crEvents");if(!host||!ev)return;
  // si el endpoint full ya trajo saldo/eventos, usar el cache (evita una request extra)
  const u=usersCache.find(x=>x.username===username);
  if(u&&u.credits!=null&&Array.isArray(u.events)){
    const d={credits:u.credits,events:u.events};
  } else {
    let d;try{d=await api("/admin/users/"+encodeURIComponent(username)+"/credits","GET",null,"admin");}
    catch(e){host.textContent="—";return;}
  }
  host.textContent=fmtCr(d.credits);
  const inp=$("#eCredits");if(inp&&inp.value==="")inp.value=String(Math.round(d.credits*100)/100);
  ev.innerHTML=`<h3 style="font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:0 0 8px">Movimientos recientes</h3>`+
    (d.events.length?`<div class="tblwrap"><table><tr><th>Δ créditos</th><th>Motivo</th><th>Fecha</th></tr>
      ${d.events.map(e=>`<tr><td class="mono" style="font-size:12px;color:${e.delta>=0?"var(--ok)":"var(--bad)"}">${e.delta>=0?"+":""}${fmtCr(e.delta)}</td>
        <td style="font-size:12px">${esc(e.reason)}</td><td class="muted" style="font-size:12px">${fdate(e.created_at)}</td></tr>`).join("")}
      </table></div>`:`<div class="empty" style="padding:6px">Sin movimientos.</div>`);
}
async function setCredits(btn){
  const u=selectedUser;if(!u){errT("elegí un usuario");return;}
  const v=parseFloat($("#eCredits").value);
  if(isNaN(v)||v<0){errT("ingresá un saldo >= 0");return;}
  busy(btn,"Guardando…");
  try{
    const r=await api("/admin/users/"+encodeURIComponent(u)+"/credits","PUT",{amount:v});
    okT(`créditos de ${u} → ${fmtCr(r.credits)}`);
    await renderUsers();
  }catch(e){errT(e.message);}
  finally{unbusy(btn);}
}
async function saveQuota(btn){
  const u=selectedUser;if(!u){errT("elegí un usuario");return;}
  busy(btn,"Guardando…");
  try{
    await api("/admin/users/"+encodeURIComponent(u)+"/quota","PUT",{
      cpu_quota:+$("#eCpu").value,ram_quota_mb:+$("#eRam").value*1024,
      disk_quota_gb:+$("#eDisk").value,max_vms:+$("#eVms").value,max_containers:+$("#eSvs").value});
    okT(`cuotas actualizadas para ${u}`);
    await renderUsers();
  }catch(e){errT(e.message);}
  finally{unbusy(btn);}
}
async function regenToken(btn,name){
  const c=name||selectedUser;if(!c){errT("elegí un usuario");return;}
  if(!await askConfirm(`Regenerar token API de «${c}»`,"El token anterior queda inválido. El token nuevo solo se muestra una vez."))return;
  busy(btn,"Generando…");
  try{
    const d=await api("/admin/clients/"+encodeURIComponent(c)+"/token","POST");
    tokenCopyToast(`Token de ${c} generado`,d.token);
  }catch(e){errT(e.message);}
  finally{unbusy(btn);}
}
async function setUserPass(name,btn){
  const pw=prompt(`Nueva contraseña para «${name}» (mín. 8 caracteres):`);
  if(!pw)return;
  if(pw.length<8){errT("mínimo 8 caracteres");return;}
  try{await api("/admin/users/"+encodeURIComponent(name)+"/password","POST",{password:pw});okT("contraseña actualizada");}
  catch(e){errT(e.message);}
}
function openUserModal(){
  closeCreate();
  const m=document.createElement("div");m.id="createModal";m.className="modal show";
  m.innerHTML=`<div class="cbox" style="width:min(560px,92vw)">
    <h3>Nuevo usuario / cliente</h3>
    <p class="muted" style="margin-top:0">Con role=client se genera el token de API (solo se muestra una vez) y las cuotas por defecto.</p>
    <div class="row">
      <div class="fld"><label>Usuario</label><input id="uName" style="margin-top:0"></div>
      <div class="fld"><label>Contraseña</label><input id="uPass" type="password" placeholder="mín. 8 caracteres"></div>
      <div class="fld"><label>Rol</label><select id="uRole"><option value="client">client</option><option value="admin">admin</option></select></div>
    </div>
    <div id="uQuotaWrap">
      <div class="row">
        <div class="fld"><label>CPU</label><input id="uCpu" type="number" value="4"></div>
        <div class="fld"><label>RAM GB</label><input id="uRam" type="number" value="8"></div>
        <div class="fld"><label>Disco GB</label><input id="uDisk" type="number" value="40"></div>
      </div>
      <div class="row">
        <div class="fld"><label>Max VMs</label><input id="uVms" type="number" value="2"></div>
        <div class="fld"><label>Max svc</label><input id="uSvs" type="number" value="3"></div>
      </div>
      <div class="fld"><label>SSH key pública (opcional)</label><textarea id="uKey" rows="3" style="font:12px var(--mono)"></textarea></div>
    </div>
    <div class="msg" id="uMsg"></div>
    <div class="cbtns">
      <button class="ghost" onclick="closeCreate()">Cancelar</button>
      <button id="uGo" onclick="createUser(this)">Crear</button>
    </div></div>`;
  const role=m.querySelector("#uRole");
  role.onchange=()=>{m.querySelector("#uQuotaWrap").style.display=role.value==="client"?"":"none";};
  m.addEventListener("click",e=>{if(e.target===m)closeCreate();});
  document.body.appendChild(m);
  setTimeout(()=>{const f=m.querySelector("#uName");if(f)f.focus();},60);
}
async function createUser(btn){
  busy(btn,"Creando…");
  try{
    const role=$("#uRole").value;
    const r=await api("/admin/users","POST",{
      username:$("#uName").value.trim(),
      password:$("#uPass").value,
      role,
      cpu_quota:+$("#uCpu").value, ram_quota_mb:+$("#uRam").value*1024, disk_quota_gb:+$("#uDisk").value,
      max_vms:+$("#uVms").value, max_containers:+$("#uSvs").value,
      ssh_key:$("#uKey").value.trim()
    });
    closeCreate();
    if(r.token){
      tokenCopyToast(`Usuario ${r.username} creado — token generado`,r.token);
    }else okT(`Usuario ${r.username} creado`);
    await renderUsers();
  }catch(e){errT(e.message);}
  finally{unbusy(btn);}
}
async function delUser(u,btn){
  if(!await askConfirm(`Borrar usuario «${u}»`,"El usuario pierde el login web y (si es client) el token API. Sus VMs siguen en PVE (borralas desde «Recursos»)."))return;
  if(btn)busy(btn);
  try{await api("/admin/users/"+encodeURIComponent(u),"DELETE");okT(u+" borrado");}
  catch(e){errT(e.message);}
  if(btn)unbusy(btn);
  await renderUsers();
}


/* ══════════ ADMIN · TICKETS ══════════ */
let adminTkCache=[],adminTkFilt="open";
const TK_FILTS=[["open","Abiertos"],["in_progress","En progreso"],["resolved","Resueltos"],["closed","Cerrados"],["all","Todos"]];
function drawTkFilt(){
  const el=$("#tkFilt"); if(!el)return;
  el.innerHTML=TK_FILTS.map(([k,l])=>`<button class="${k===adminTkFilt?"on":""}" onclick="setTkFilt('${k}')">${l}</button>`).join("");
}
function setTkFilt(v){adminTkFilt=v;drawTkFilt();drawTkList(adminTkCache);}
function drawTkList(rows){
  const vis=adminTkFilt==="all"?rows:rows.filter(t=>t.status===adminTkFilt);
  $("#tkList").innerHTML=vis.length
    ?`<div class="tblwrap"><table><tr><th>#</th><th>Usuario</th><th>Título</th><th>Categoría</th><th>Estado</th><th>Resp.</th><th>Actualizado</th></tr>
     ${vis.map(t=>`<tr style="cursor:pointer" onclick="openAdminTicket(${t.id})">
       <td class="mono" style="font-size:12px">#${t.id}</td>
       <td>${esc(t.username)}</td>
       <td>${esc(t.title)}</td>
       <td class="muted" style="font-size:12px">${catLabel(t.category)}</td>
       <td>${ticketPill(t.status)}</td>
       <td class="muted" style="font-size:12px">${t.n_replies}</td>
       <td class="muted" style="font-size:12px">${fdate(t.updated_at)}</td>
     </tr>`).join("")}
     </table></div>`
    :`<div class="empty">Sin tickets ${adminTkFilt==="all"?"":`en «${TK_FILTS.find(x=>x[0]===adminTkFilt)[1]}»`}.
      ${adminTkFilt==="open"?"":"<span class='muted'>Cambiar el filtro para ver el resto.</span>"}</div>`;
}
async function renderTickets(){
  const d=await api("/admin/tickets","GET",null,"admin");
  adminTkCache=d.tickets; drawTkFilt(); drawTkList(d.tickets);
}
async function openAdminTicket(tid){
  let t;try{t=await api("/admin/tickets/"+tid,"GET",null,"admin");}catch(e){errT(e.message);return;}
  const dr=$("#admDrawer");if(dr)dr.classList.add("open");
  const tt=$("#edTitle");if(tt)tt.textContent="Ticket #"+t.id;
  const body=$("#edBody");if(!body)return;
  const btn=(st,cur)=>`<button class="${cur===st?"":"ghost"}" onclick="tkSetStatus(${t.id},'${st}',this)" ${cur===st?'disabled style="opacity:.5"':''}>${TICKET_ST[st]}</button>`;
  body.innerHTML=`
    <div class="qrow" style="margin-top:0;align-items:center;gap:8px"><b style="flex:1">${esc(t.title)}</b>${ticketPill(t.status)}</div>
    <div class="hint" style="margin-top:6px">Usuario: <b>${esc(t.username)}</b> · ${catLabel(t.category)} · ${fdate(t.created_at)}${t.updated_at!==t.created_at?` · actualizado ${fdate(t.updated_at)}`:""}</div>
    <pre style="white-space:pre-wrap;font:13px var(--mono);background:#0d1420;padding:10px;border-radius:8px;margin:10px 0">${esc(t.description)}</pre>
    <div class="row" style="margin-top:10px;flex-wrap:wrap">
      ${btn("open",t.status)}${btn("in_progress",t.status)}${btn("resolved",t.status)}${btn("closed",t.status)}
    </div>
    <div class="fld mt"><label>Nota (visible para el usuario en su reporte)</label>
      <textarea id="tkNote" rows="3" placeholder="contexto / resolución…">${esc(t.admin_note||"")}</textarea></div>
    <div class="row"><button class="ghost" onclick="saveTkNote(${t.id},this)">Guardar nota</button></div>
    <div class="mt" style="border-top:1px solid var(--line);padding-top:14px">
      <h3 style="font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:0 0 8px">Hilo</h3>
      <div id="tkReplies">${t.replies.map(r=>`
        <div style="margin-bottom:10px"><div class="hint" style="margin:0 0 2px">${r.username==="admin"?"<b>Admin</b>":esc(r.username)} · ${fdate(r.created_at)}</div>
        <pre style="white-space:pre-wrap;font:13px var(--mono);background:#0d1420;padding:8px;border-radius:8px;margin:0">${esc(r.body)}</pre></div>`).join("")
        ||`<div class="empty" style="padding:6px">Sin mensajes.</div>`}</div>
      ${t.status!=="closed"
        ?`<div class="fld mt"><label>Escribir al usuario</label><textarea id="tkReply" rows="3"></textarea></div>
          <div class="row"><button class="ghost" onclick="tkReply(${t.id},this)">Enviar al usuario</button></div>`
        :""}
    </div>`;
}
async function tkSetStatus(tid,st,btn){
  if(btn)busy(btn,"Guardando…");
  try{
    await api("/admin/tickets/"+tid,"PATCH",{status:st},null,"admin");
    okT(`ticket #${tid} → ${TICKET_ST[st]}`);
    openAdminTicket(tid);
    setTimeout(renderTickets,500);
  }catch(e){errT(e.message);}
  finally{if(btn)unbusy(btn);}
}
async function saveTkNote(tid,btn){
  busy(btn,"Guardando…");
  try{
    await api("/admin/tickets/"+tid,"PATCH",{admin_note:$("#tkNote").value},null,"admin");
    okT("nota guardada");
  }catch(e){errT(e.message);}
  finally{unbusy(btn);}
}
async function tkReply(tid,btn){
  busy(btn,"Enviando…");
  try{
    await api(`/admin/tickets/${tid}/replies`,"POST",{body:$("#tkReply").value},null,"admin");
    okT("mensaje enviado al usuario");
    openAdminTicket(tid);
    setTimeout(renderTickets,500);
  }catch(e){errT(e.message);}
  finally{unbusy(btn);}
}
