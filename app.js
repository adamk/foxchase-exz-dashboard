(() => {
  const cfg=window.ZWAP_CONFIG||{};
  const $=id=>document.getElementById(id);
  const status=t=>{$('status').textContent=t};
  const fmt=v=>v==null||Number.isNaN(Number(v))?'—':Number(v).toFixed(2);
  const etDate=()=>{const parts=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());const get=k=>parts.find(v=>v.type===k).value;return `${get('year')}-${get('month')}-${get('day')}`};
  const etTime=value=>{const d=new Date(value);return Number.isNaN(d.getTime())?'':new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'}).format(d)};
  const etAxisTime=value=>{const d=new Date(value);if(Number.isNaN(d.getTime()))return '';return new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'}).format(d)};
  const sessionId=(()=>{const key='foxchase_zwap_session';let v=localStorage.getItem(key);if(!v){v=crypto.randomUUID().replaceAll('-','');localStorage.setItem(key,v)}return v})();
  const activationUrl=cfg.activationUrl||'https://exz-api.foxchasetrading.com/api/public/exz/activate';
  const liveComputeUrl=cfg.liveComputeUrl||'https://exz-api.foxchasetrading.com/api/public/exz/live';
  const liveRefreshMs=Math.max(15000,Number(cfg.liveRefreshMs||30000));
  const liveTokenKey='foxchase_exz_live_token';
  const liveExpiryKey='foxchase_exz_live_expires';
  const liveLicenseExpiryKey='foxchase_exz_license_expires';
  const regimeCacheKey='foxchase_exz_regimes';
  let cachedRegimes={};
  try{cachedRegimes=JSON.parse(localStorage.getItem(regimeCacheKey)||'{}')||{}}catch(_){cachedRegimes={}}
  let sessions=[];
  let liveRefreshTimer=null;
  let loadInFlight=false;
  const sessionsUrl=cfg.sessionsUrl||(cfg.connectorUrl||'').replace(/\/api\/session$/,'/api/sessions');
  function liveToken(){const token=localStorage.getItem(liveTokenKey),expires=Number(localStorage.getItem(liveExpiryKey)||0);if(!token||!expires||Date.now()>=expires){localStorage.removeItem(liveTokenKey);localStorage.removeItem(liveExpiryKey);return ''}return token}
  function stopLiveRefresh(){if(liveRefreshTimer!==null){clearInterval(liveRefreshTimer);liveRefreshTimer=null}}
  function scheduleLiveRefresh(){
    stopLiveRefresh();
    if($('date').value!==etDate()||!liveToken())return;
    liveRefreshTimer=setInterval(()=>{
      if($('date').value!==etDate()||!liveToken()){stopLiveRefresh();return}
      load({automatic:true});
    },liveRefreshMs);
  }
  function updateLiveStatus(){const el=$('liveStatus');if(!el)return;const expires=Number(localStorage.getItem(liveExpiryKey)||0),licenseExpires=Number(localStorage.getItem(liveLicenseExpiryKey)||0),token=liveToken(),licenseText=licenseExpires?`valid through ${new Intl.DateTimeFormat('en-US',{month:'short',day:'numeric',year:'numeric'}).format(new Date(licenseExpires))}`:'active';el.textContent=token?`Live access ${licenseText} · session active until ${new Date(expires).toLocaleTimeString()}`:licenseExpires?`Live access ${licenseText} · activate a session to use it`:'Not activated'}
  function setupCanvas(id){const c=$(id),dpr=devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight;c.width=w*dpr;c.height=h*dpr;const x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);return{x,w,h}};
  function chartSeries(series){
    let lastTime=NaN;
    return series.map((point,index)=>{
      let timestamp=Date.parse(point.timestamp);
      if(!Number.isFinite(timestamp)||(Number.isFinite(lastTime)&&timestamp<=lastTime)){
        timestamp=Number.isFinite(lastTime)?lastTime+60*1000:timestamp;
      }
      if(Number.isFinite(timestamp))lastTime=timestamp;
      return Number.isFinite(timestamp)?{...point,timestamp:new Date(timestamp).toISOString()}:point;
    });
  }
  function drawGrid(x,l,t,r,b,lo,hi){x.font='10px sans-serif';for(let i=0;i<=4;i++){const y=t+(b-t)*i/4,v=hi-(hi-lo)*i/4;x.strokeStyle='#252b33';x.beginPath();x.moveTo(l,y);x.lineTo(r,y);x.stroke();x.fillStyle='#8b949e';x.textAlign='right';x.fillText(v.toFixed(2),r-4,y+3)}}
  function drawTimeAxis(x,p,l,r,b,t){
    const indices=new Map(),add=(i,label=null)=>{if(i>=0&&!indices.has(i))indices.set(i,label)};
    const times=p.map(v=>Date.parse(v.timestamp));
    const valid=times.filter(Number.isFinite),first=valid[0],last=valid[valid.length-1];
    add(0,etAxisTime(p[0].timestamp));
    if(Number.isFinite(first)&&Number.isFinite(last)){
      const quarter=15*60*1000;
      for(let target=Math.ceil(first/quarter)*quarter;target<=last;target+=quarter){
        let best=-1,bestDistance=Infinity;
        times.forEach((value,i)=>{if(!Number.isFinite(value))return;const distance=Math.abs(value-target);if(distance<bestDistance){best=i;bestDistance=distance}});
        if(best>=0&&bestDistance<=8*60*1000)add(best,etAxisTime(new Date(target)));
      }
    }
    const count=Math.min(6,p.length);
    for(let j=1;j<count;j++)add(Math.round((p.length-1)*j/Math.max(1,count-1)),null);
    add(p.length-1,etAxisTime(p[p.length-1].timestamp));
    const entries=[...indices.entries()].sort((a,b)=>a[0]-b[0]);
    x.font='10px sans-serif';x.strokeStyle='#30363d';x.fillStyle='#8b949e';x.setLineDash([]);
    const labels=new Set();
    for(const [j,[i,requestedLabel]] of entries.entries()){
      const label=requestedLabel||etAxisTime(p[i].timestamp);if(!label||labels.has(label))continue;labels.add(label);
      const cx=l+(r-l)*i/Math.max(1,p.length-1);x.beginPath();x.moveTo(cx,b+1);x.lineTo(cx,b+5);x.stroke();x.textAlign=j===0?'left':j===entries.length-1?'right':'center';x.fillText(label,cx,b+17)
    }
  }
  function drawPrice(series){const {x,w,h}=setupCanvas('price'),p=chartSeries(series).filter(v=>v.option_close!=null);if(!p.length)return;const l=10,r=w-10,t=10,b=h-38,vals=p.flatMap(v=>[v.option_low??v.option_close,v.option_high??v.option_close]);let lo=Math.min(...vals),hi=Math.max(...vals),pad=Math.max((hi-lo)*.12,.05);lo-=pad;hi+=pad;const X=i=>l+(r-l)*i/Math.max(1,p.length-1),Y=v=>b-(b-t)*(v-lo)/(hi-lo),candleWidth=Math.max(3,Math.min(12,(r-l)/Math.max(1,p.length)*.72));drawGrid(x,l,t,r,b,lo,hi);drawTimeAxis(x,p,l,r,b,t);p.forEach((v,i)=>{const o=Number(v.option_open??v.option_close),c=Number(v.option_close),color=c>=o?'#39ff14':'#ff391f',cx=X(i);x.strokeStyle=color;x.beginPath();x.moveTo(cx,Y(Number(v.option_high??c)));x.lineTo(cx,Y(Number(v.option_low??c)));x.stroke();x.fillStyle=color;const top=Y(Math.max(o,c)),bottom=Y(Math.min(o,c));x.fillRect(cx-candleWidth/2,top,candleWidth,Math.max(1,bottom-top))})}
  function drawZ(series){const {x,w,h}=setupCanvas('z'),p=chartSeries(series).filter(v=>v.ex_z!=null);if(!p.length)return;const l=10,r=w-10,t=10,b=h-38,ref=p.slice(0,30).map(v=>Number(v.ex_z)),lo=Math.min(-2,...ref)-.1,hi=Math.max(2,...ref)+.1,X=i=>l+(r-l)*i/Math.max(1,p.length-1),Y=v=>b-(b-t)*(v-lo)/(hi-lo);drawGrid(x,l,t,r,b,lo,hi);drawTimeAxis(x,p,l,r,b,t);x.setLineDash([4,4]);x.strokeStyle='#6e7681';[0].forEach(v=>{x.beginPath();x.moveTo(l,Y(v));x.lineTo(r,Y(v));x.stroke()});x.setLineDash([]);x.strokeStyle='#f2cc60';x.lineWidth=2;x.beginPath();p.forEach((v,i)=>i?x.lineTo(X(i),Y(Number(v.ex_z))):x.moveTo(X(i),Y(Number(v.ex_z))));x.stroke()}
  function sessionRegime(s){return (s.regime==='UNKNOWN'&&cachedRegimes[s.date])||s.regime||'UNKNOWN'}
  function renderSessions(){const filter=$('regimeFilter').value;const current=$('date').value;const visible=sessions.filter(s=>s.date<etDate()&&(filter==='ALL'||sessionRegime(s)===filter));const picker=$('sessionPicker');picker.innerHTML=visible.length?visible.map(s=>`<option value="${s.date}" ${s.date===current?'selected':''}>${s.date} · ${sessionRegime(s)}</option>`).join(''):'<option value="">No cached historical sessions</option>';if(current&&visible.some(s=>s.date===current))picker.value=current}
  async function refreshSessions(){if(!sessionsUrl)return;try{const response=await fetch(sessionsUrl);const result=await response.json();if(response.ok&&Array.isArray(result.sessions)){sessions=result.sessions;renderSessions()}}catch(_) {}}
  async function heartbeat(){if(!cfg.presenceUrl)return;try{await fetch(cfg.presenceUrl,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sessionId})})}catch(_){}}
  async function activate(){const code=$('activationCode').value.trim();if(!code){status('Enter your Whop license key or complimentary activation code.');return}$('activate').disabled=true;status('Checking live access…');try{const response=await fetch(activationUrl,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({activation_code:code})});const result=await response.json();if(!response.ok)throw new Error(result.error||'activation failed');localStorage.setItem(liveTokenKey,result.access_token);localStorage.setItem(liveExpiryKey,String(Date.parse(result.expires_at)));if(result.license_expires_at)localStorage.setItem(liveLicenseExpiryKey,String(Date.parse(result.license_expires_at)));else localStorage.removeItem(liveLicenseExpiryKey);$('activationCode').value='';updateLiveStatus();status('Live access activated. Select today and press Load session.')}catch(e){status(`Activation error: ${e.message}`)}finally{$('activate').disabled=false}}
  async function load(options={}){
    if(loadInFlight)return;
    const date=$('date').value,offset=Number($('offset').value||1);
    if(!date){status('Choose a date first.');return}
    const isLiveDate=date===etDate();
    if(date>etDate()){stopLiveRefresh();status('Live access is limited to the current trading day.');return}
    const token=isLiveDate?liveToken():'';
    if(isLiveDate&&!token){stopLiveRefresh();status('Current-day access requires an active Foxchase EXZ Live entitlement.');return}
    if(!isLiveDate)stopLiveRefresh();
    const computeUrl=isLiveDate?liveComputeUrl:cfg.computeUrl;
    if(!cfg.connectorUrl||!computeUrl){status('Local test config is incomplete. Copy config.example.js to config.js.');return}
    loadInFlight=true;
    $('load').disabled=true;
    status(options.automatic?'Refreshing current-day Alpaca data…':(isLiveDate?'Fetching current-day Alpaca data locally…':'Fetching your Alpaca data locally…'));
    try{
      const source=await fetch(`${cfg.connectorUrl}?date=${encodeURIComponent(date)}&offset=${offset}${isLiveDate?'&live=1':''}`);
      const payload=await source.json();
      if(!source.ok)throw new Error(payload.error||'local connector failed');
      status('Computing the study…');
      const headers={'Content-Type':'application/json','X-ZWAP-Session':sessionId};
      if(isLiveDate)headers.Authorization=`Bearer ${token}`;else if(cfg.computeToken)headers.Authorization=`Bearer ${cfg.computeToken}`;
      const response=await fetch(computeUrl,{method:'POST',headers,body:JSON.stringify(payload)});
      const result=await response.json();
      if(!response.ok){
        if(isLiveDate&&response.status===401){localStorage.removeItem(liveTokenKey);localStorage.removeItem(liveExpiryKey);updateLiveStatus();stopLiveRefresh()}
        throw new Error(result.error||'calculation failed')
      }
      const p=result.series||[],z=p.filter(v=>v.ex_z!=null);
      $('contract').textContent=result.option_symbol||'—';$('regime').textContent=result.regime||'—';
      if(result.regime&&result.regime!=='UNKNOWN'){cachedRegimes[date]=result.regime;localStorage.setItem(regimeCacheKey,JSON.stringify(cachedRegimes));renderSessions()}
      $('latest').textContent=z.length?fmt(z[z.length-1].ex_z):'—';drawPrice(p);drawZ(p);
      status(isLiveDate?`Updated ${result.option_symbol||'session'} · live auto-refresh every ${Math.round(liveRefreshMs/1000)}s.`:`Loaded ${result.option_symbol||'session'} · Study ready.`);
      if(isLiveDate)scheduleLiveRefresh();
      refreshSessions();
    }catch(e){status(`Error: ${e.message}`)}finally{$('load').disabled=false;loadInFlight=false}
  }
  $('date').max=etDate();
  $('regimeFilter').addEventListener('change',renderSessions);
  $('sessionPicker').addEventListener('change',()=>{if($('sessionPicker').value){$('date').value=$('sessionPicker').value;load()}});
  $('load').addEventListener('click',load);
  $('activate').addEventListener('click',activate);
  document.querySelectorAll('.strike-button').forEach(button=>button.addEventListener('click',()=>{
    const next=button.dataset.offset!==undefined?Number(button.dataset.offset):Number($('offset').value||0)+Number(button.dataset.step||0);
    $('offset').value=Math.max(-10,Math.min(10,next));
    if($('date').value) load();
  }));
  refreshSessions();heartbeat();updateLiveStatus();setInterval(heartbeat,30000);addEventListener('resize',()=>{});
})();
