/* Optional published-snapshot metadata only; no fetching, calculation or licensing. */
(function(root){
  function describe(offsets){
    if(!Array.isArray(offsets)||offsets.some(n=>!Number.isInteger(n)||n < -10||n > 10))return 'Published historical offset availability is not reported by this connector.';
    const values=[...new Set(offsets)].sort((a,b)=>a-b);
    return values.length?'Published historical offsets: '+values.map(n=>n>0?'+'+n:String(n)).join(', ')+'. Other offsets may be unavailable.':'No published historical offsets are available for this session.';
  }
  const api={describe};if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.FoxchaseHistoricalOffsets=api;
})(typeof globalThis!=='undefined'?globalThis:this);
