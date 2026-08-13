(() => {
  const cfg=window.ZWAP_CONFIG||{};
  const $=id=>document.getElementById(id);
  const status=t=>{$('status').textContent=t};
  const fmt=v=>v==null||Number.isNaN(Number(v))?'—':Number(v).toFixed(2);
  const etDate=()=>{const parts=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());const get=k=>parts.find(v=>v.type===k).value;return `${get('year')}-${get('month')}-${get('day')}`};
  const etTime=value=>{const d=new Date(value);return Number.isNaN(d.getTime())?'':new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'}).format(d)};
  const etMinutes=value=>{const d=new Date(value);if(Number.isNaN(d.getTime()))return NaN;const parts=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hour12:false}).formatToParts(d);const hour=Number(parts.find(v=>v.type==='hour')?.value),minute=Number(parts.find(v=>v.type==='minute')?.value);return Number.isFinite(hour)&&Number.isFinite(minute)?hour*60+minute:NaN};
  const etAxisTime=value=>{const d=new Date(value);if(Number.isNaN(d.getTime()))return '';return new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'}).format(d)};
  const sessionId=(()=>{const key='foxchase_zwap_session';let v=localStorage.getItem(key);if(!v){v=crypto.randomUUID().replaceAll('-','');localStorage.setItem(key,v)}return v})();
  const activationUrl=cfg.activationUrl||'https://exz-api.foxchasetrading.com/api/public/exz/activate';
  const liveComputeUrl=cfg.liveComputeUrl||'https://exz-api.foxchasetrading.com/api/public/exz/live';
  const rvolUrl=cfg.rvolUrl||'https://exz-api.foxchasetrading.com/api/public/exz/rvol';
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
  let renderedSeries=null;
  let renderedRvol=null;
  let crosshairIndex=null;
  const zScaleKey='foxchase_exz_z_scale';
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
  function chartLayout(series,l,r){
    const step=Math.min(14,(r-l)/Math.max(1,series.length-1));
    const plotRight=Math.min(r,l+step*Math.max(1,series.length-1));
    const indexByTimestamp=new Map(series.map((point,index)=>[point.timestamp,index]));
    const XIndex=index=>l+(plotRight-l)*index/Math.max(1,series.length-1);
    const XPoint=point=>XIndex(indexByTimestamp.get(point.timestamp)??0);
    return {plotRight,XIndex,XPoint};
  }
  function drawGrid(x,l,t,r,b,lo,hi){x.font='10px sans-serif';for(let i=0;i<=4;i++){const y=t+(b-t)*i/4,v=hi-(hi-lo)*i/4;x.strokeStyle='#252b33';x.beginPath();x.moveTo(l,y);x.lineTo(r,y);x.stroke();x.fillStyle='#8b949e';x.textAlign='right';x.fillText(v.toFixed(2),r-4,y+3)}}
  function drawTimeAxis(x,p,l,r,b,t){
    // Use quarter-hour labels only. The former mixture of quarter-hour and
    // evenly-spaced labels could place two different timestamps on top of one
    // another (most noticeably around 10:00), making the axis look blurred.
    const indices=new Map(),add=(i,label=null)=>{if(i>=0&&!indices.has(i))indices.set(i,label)};
    const times=p.map(v=>Date.parse(v.timestamp));
    const valid=times.filter(Number.isFinite),first=valid[0],last=valid[valid.length-1];
    if(!p.length)return;
    add(0,etAxisTime(p[0].timestamp));
    if(Number.isFinite(first)&&Number.isFinite(last)){
      const quarter=15*60*1000;
      for(let target=Math.ceil(first/quarter)*quarter;target<=last;target+=quarter){
        let best=-1,bestDistance=Infinity;
        times.forEach((value,i)=>{if(!Number.isFinite(value))return;const distance=Math.abs(value-target);if(distance<bestDistance){best=i;bestDistance=distance}});
        // A normal one-minute session should have a bar very close to the
        // requested quarter-hour. Keep a generous tolerance for sparse data.
        if(best>=0&&bestDistance<=8*60*1000)add(best,etAxisTime(new Date(target)));
      }
    }
    add(p.length-1,etAxisTime(p[p.length-1].timestamp));
    const entries=[...indices.entries()].sort((a,b)=>a[0]-b[0]);
    x.font='10px sans-serif';x.strokeStyle='#30363d';x.fillStyle='#8b949e';x.setLineDash([]);
    const labels=new Set();
    let lastLabelRight=-Infinity;
    const minLabelGap=42;
    for(const [j,[i,requestedLabel]] of entries.entries()){
      const label=requestedLabel||etAxisTime(p[i].timestamp);if(!label||labels.has(label))continue;
      const cx=l+(r-l)*i/Math.max(1,p.length-1);
      const isFirst=j===0,isLast=j===entries.length-1;
      const width=x.measureText(label).width;
      const left=isFirst?cx:isLast?cx-width:cx-width/2;
      const right=isFirst?cx+width:isLast?cx:cx+width/2;
      // Do not force the final, non-quarter-hour timestamp into the space
      // occupied by the preceding label. That made the right edge look like
      // blurred text as live bars advanced toward the next quarter hour.
      if(!isFirst&&left<lastLabelRight+minLabelGap)continue;
      labels.add(label);lastLabelRight=right;
      x.beginPath();x.moveTo(cx,b+1);x.lineTo(cx,b+5);x.stroke();
      x.textAlign=isFirst?'left':isLast?'right':'center';x.fillText(label,cx,b+17)
    }
  }
  function drawPrice(series){const {x,w,h}=setupCanvas('price'),p=series.filter(v=>v.option_close!=null);if(!p.length)return;const l=10,r=w-10,t=10,b=h-38,vals=p.flatMap(v=>[v.option_low??v.option_close,v.option_high??v.option_close]);let lo=Math.min(...vals),hi=Math.max(...vals),pad=Math.max((hi-lo)*.12,.05);lo-=pad;hi+=pad;const {plotRight,XPoint}=chartLayout(series,l,r),Y=v=>b-(b-t)*(v-lo)/(hi-lo),step=(plotRight-l)/Math.max(1,series.length-1),candleWidth=Math.max(3,Math.min(10,step*.72));drawGrid(x,l,t,r,b,lo,hi);drawTimeAxis(x,series,l,plotRight,b,t);p.forEach(v=>{const o=Number(v.option_open??v.option_close),c=Number(v.option_close),color=c>=o?'#39ff14':'#ff391f',cx=XPoint(v);x.strokeStyle=color;x.beginPath();x.moveTo(cx,Y(Number(v.option_high??c)));x.lineTo(cx,Y(Number(v.option_low??c)));x.stroke();x.fillStyle=color;const top=Y(Math.max(o,c)),bottom=Y(Math.min(o,c));x.fillRect(cx-candleWidth/2,top,candleWidth,Math.max(1,bottom-top))})}
  function drawZ(series){
    const {x,w,h}=setupCanvas('z'),p=series.filter(v=>v.ex_z!=null);if(!p.length)return;
    // Size the axis from the complete morning (09:30–12:00 ET), not merely
    // the first 30 returned points. This captures morning highs/lows even when
    // the feed is sparse or starts after the opening bar, while later-session
    // spikes remain clamped and reported as overflow.
    const scaleMode=$('zScale')?.value==='session'?'session':'morning',morning=p.filter(v=>{const m=etMinutes(v.timestamp);return m>=570&&m<720}),reference=scaleMode==='session'?p:(morning.length>=3?morning:p.slice(0,30)),ref=reference.map(v=>Number(v.ex_z)),
      l=10,r=w-10,t=10,b=h-48,lo=Math.min(-2,...ref)-.1,hi=Math.max(2,...ref)+.1;
    const {plotRight,XPoint}=chartLayout(series,l,r),X=v=>XPoint(v),Y=v=>b-(b-t)*(v-lo)/(hi-lo),plotY=v=>Math.max(t,Math.min(b,Y(v)));
    drawGrid(x,l,t,r,b,lo,hi);drawTimeAxis(x,series,l,plotRight,b,t);
    x.setLineDash([4,4]);[-1,0,1].forEach(v=>{x.beginPath();x.strokeStyle=v===0?'#9da7b3':'#58616d';x.lineWidth=v===0?1.5:1;x.moveTo(l,plotY(v));x.lineTo(plotRight,plotY(v));x.stroke()});x.setLineDash([]);x.lineWidth=1;
    x.save();x.beginPath();x.rect(l,t,plotRight-l,b-t);x.clip();x.strokeStyle='#f2cc60';x.lineWidth=2;x.beginPath();
    p.forEach((v,i)=>i?x.lineTo(X(v),plotY(Number(v.ex_z))):x.moveTo(X(v),plotY(Number(v.ex_z))));x.stroke();x.restore();
  }
  function drawRvol(series,rvolSeries){
    const {x,w,h}=setupCanvas('rvol'),p=(rvolSeries||[]).filter(v=>v.rvol!=null&&Number.isFinite(Date.parse(v.timestamp))).sort((a,b)=>Date.parse(a.timestamp)-Date.parse(b.timestamp));if(!p.length)return;
    const l=10,r=w-10,t=10,b=h-38,values=p.map(v=>Number(v.rvol)),hi=Math.max(2.4,...values)*1.08,lo=0;
    const colors={purple:'#a371f7',red:'#ff391f',orange:'#ffa200',green:'#39bd44',blue:'#00c1e3',grey:'#8b949e'};
    // rVol belongs to SPY, not to the selected option. Give each Eastern-time
    // five-minute bucket its own fixed RTH slot so sparse option bars cannot
    // collapse or stretch the volume bars.
    const lastMinute=Math.max(570,...p.map(v=>etMinutes(v.timestamp)).filter(Number.isFinite)),visibleMinutes=Math.max(5,lastMinute-570+5),slotWidth=(r-l)*5/visibleMinutes,Y=v=>b-(b-t)*(v-lo)/(hi-lo),X=v=>l+(r-l)*Math.max(0,Math.min(visibleMinutes,etMinutes(v.timestamp)-570))/visibleMinutes;
    drawGrid(x,l,t,r,b,lo,hi);
    // The shared time-axis helper spaces labels by array index. rVol instead
    // occupies absolute five-minute RTH slots, so its labels must use the same
    // clock coordinate or they appear irregularly spaced.
    x.font='10px sans-serif';x.strokeStyle='#30363d';x.fillStyle='#8b949e';x.textAlign='center';
    for(let minute=570;minute<=lastMinute;minute+=30){
      const cx=l+(r-l)*(minute-570)/visibleMinutes,hour=Math.floor(minute/60),mins=minute%60,displayHour=((hour+11)%12)+1,label=`${displayHour}:${String(mins).padStart(2,'0')} ${hour>=12?'PM':'AM'}`;
      x.beginPath();x.moveTo(cx,b+1);x.lineTo(cx,b+5);x.stroke();x.fillText(label,cx,b+17)
    }
    x.setLineDash([4,4]);x.strokeStyle='#8b949e';x.beginPath();x.moveTo(l,Y(1));x.lineTo(r,Y(1));x.stroke();x.setLineDash([]);
    p.forEach(v=>{const value=Number(v.rvol),left=X(v),width=Math.max(1,slotWidth*.9);x.fillStyle=colors[v.color]||colors.grey;x.fillRect(left,Y(value),width,Math.max(1,b-Y(value)))})
  }
  function drawCrosshair(series,index){
    if(!series||!Number.isInteger(index)||index<0||index>=series.length)return;
    const timeLabel=etAxisTime(series[index].timestamp);
    [['price',38],['z',48],['rvol',38]].forEach(([id,bottomPad])=>{
      const canvas=$(id),w=canvas.clientWidth,h=canvas.clientHeight,dpr=devicePixelRatio||1,ctx=canvas.getContext('2d');
      ctx.setTransform(dpr,0,0,dpr,0,0);const l=10,r=w-10,{plotRight}=chartLayout(series,l,r),cx=l+(plotRight-l)*index/Math.max(1,series.length-1),plotBottom=h-bottomPad;
      ctx.save();ctx.beginPath();ctx.rect(l,10,plotRight-l,h-bottomPad-10);ctx.clip();ctx.strokeStyle='#c9d1d9';ctx.lineWidth=1;ctx.setLineDash([3,3]);ctx.beginPath();ctx.moveTo(cx,10);ctx.lineTo(cx,h-bottomPad);ctx.stroke();ctx.restore();
      if(timeLabel){
        ctx.save();ctx.setLineDash([]);ctx.font='600 11px sans-serif';const padX=6,labelWidth=ctx.measureText(timeLabel).width+padX*2,labelHeight=20;
        const labelX=Math.max(l,Math.min(plotRight-labelWidth,cx-labelWidth/2)),labelY=plotBottom+3;
        ctx.fillStyle='#c9d1d9';ctx.fillRect(labelX,labelY,labelWidth,labelHeight);
        ctx.fillStyle='#0d1117';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(timeLabel,labelX+labelWidth/2,labelY+labelHeight/2);ctx.restore();
      }
    });
  }
  function redrawWithCrosshair(){if(!renderedSeries)return;drawPrice(renderedSeries);drawZ(renderedSeries);drawRvol(renderedSeries,renderedRvol);drawCrosshair(renderedSeries,crosshairIndex)}
  function updateCrosshair(event){
    if(!renderedSeries)return;const canvas=event.currentTarget,rect=canvas.getBoundingClientRect(),x=Math.max(10,Math.min(canvas.clientWidth-10,event.clientX-rect.left)),l=10,r=canvas.clientWidth-10,{plotRight}=chartLayout(renderedSeries,l,r);
    crosshairIndex=Math.max(0,Math.min(renderedSeries.length-1,Math.round((x-l)/Math.max(1,plotRight-l)*(renderedSeries.length-1))));redrawWithCrosshair();
  }
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
    $('rvolSection').hidden=true;
    if(!isLiveDate)renderedRvol=[];
    if(date>etDate()){stopLiveRefresh();status('Live access is limited to the current trading day.');return}
    const token=isLiveDate?liveToken():'';
    if(isLiveDate&&!token){stopLiveRefresh();status('Current-day access requires an active EXZ Live entitlement from Foxchase Trading.');return}
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
      let rvolSeries=[];
      if(isLiveDate&&rvolUrl){
        try{const rvolResponse=await fetch(`${rvolUrl}?date=${encodeURIComponent(date)}`,{headers:{Authorization:`Bearer ${token}`}}),rvolResult=await rvolResponse.json();if(rvolResponse.ok&&Array.isArray(rvolResult.series))rvolSeries=rvolResult.series}catch(_){}
      }
      $('rvolSection').hidden=!(isLiveDate&&rvolSeries.length);
      $('latest').textContent=z.length?fmt(z[z.length-1].ex_z):'—';const visualSeries=chartSeries(p);renderedSeries=visualSeries;renderedRvol=rvolSeries;crosshairIndex=null;drawPrice(visualSeries);drawZ(visualSeries);if(isLiveDate)drawRvol(visualSeries,renderedRvol);
      status(isLiveDate?`Updated ${result.option_symbol||'session'} · live auto-refresh every ${Math.round(liveRefreshMs/1000)}s.`:`Loaded ${result.option_symbol||'session'} · Study ready.`);
      if(isLiveDate)scheduleLiveRefresh();
      refreshSessions();
    }catch(e){status(`Error: ${e.message}`)}finally{$('load').disabled=false;loadInFlight=false}
  }
  $('date').max=etDate();
  $('date').addEventListener('change',()=>{$('offset').value=0});
  ['price','z','rvol'].forEach(id=>{const canvas=$(id);canvas.addEventListener('pointermove',updateCrosshair);canvas.addEventListener('pointerleave',()=>{crosshairIndex=null;redrawWithCrosshair()})});
  $('regimeFilter').addEventListener('change',renderSessions);
  if($('zScale')){
    const savedScale=localStorage.getItem(zScaleKey);if(savedScale==='session'||savedScale==='morning')$('zScale').value=savedScale;
    $('zScale').addEventListener('change',()=>{localStorage.setItem(zScaleKey,$('zScale').value);$('zScaleNote').textContent=$('zScale').value==='session'?'Full session auto includes all available z-score highs and lows.':'Morning anchored keeps the scale stable after noon; switch to Full session auto to study afternoon extremes.';redrawWithCrosshair()});
  }
  $('sessionPicker').addEventListener('change',()=>{if($('sessionPicker').value){$('date').value=$('sessionPicker').value;$('offset').value=0;load()}});
  $('load').addEventListener('click',load);
  $('activate').addEventListener('click',activate);
  document.querySelectorAll('.strike-button').forEach(button=>button.addEventListener('click',()=>{
    const next=button.dataset.offset!==undefined?Number(button.dataset.offset):Number($('offset').value||0)+Number(button.dataset.step||0);
    $('offset').value=Math.max(-10,Math.min(10,next));
    if($('date').value) load();
  }));
  refreshSessions();heartbeat();updateLiveStatus();setInterval(heartbeat,30000);addEventListener('resize',()=>{});
})();
