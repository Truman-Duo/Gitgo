/* Handoff reference. Original topology layout; Serie's screenshots are the visual reference.
 * First-parent chains own persistent lanes. Only forks/merges change lanes.
 * Coordinates are horizontal: chronological/topological columns, branch rows.
 */
function commitLayout(input, primaryOid, extraRefs={}) {
  const commits=[...input].reverse().map((c,index)=>({...c,index,lane:null,labels:[...c.refs,...(extraRefs[c.oid]||[])]}));
  const by=new Map(commits.map(c=>[c.oid,c])),lanes=[];
  function assign(oid,label){
    if(!by.has(oid)||by.get(oid).lane!==null)return;
    const lane=lanes.length;lanes.push({lane,label,tip:oid});
    let node=by.get(oid);
    while(node&&node.lane===null){node.lane=lane;node=by.get(node.parents[0]);}
  }
  assign(primaryOid,by.get(primaryOid)?.labels.find(r=>!r.startsWith('tag:'))||'Release');
  const tips=commits.filter(c=>c.labels.some(r=>!r.startsWith('tag:'))).sort((a,b)=>{
    const priority=c=>c.labels.some(r=>r.startsWith('integration/'))?0:c.labels.includes('trial')?1:c.labels.includes('workspace')?2:3;
    return priority(a)-priority(b)||a.labels.join().localeCompare(b.labels.join());
  });
  for(const tip of tips)assign(tip.oid,tip.labels.find(r=>!r.startsWith('tag:')));
  for(const c of [...commits].reverse())assign(c.oid,c.labels[0]||'history '+c.oid.slice(0,7));
  const edges=commits.flatMap(child=>child.parents.map((oid,slot)=>({parent:by.get(oid),child,slot})).filter(e=>e.parent));
  return {commits,lanes,edges,boundary:commits.filter(c=>c.parents.some(p=>!by.has(p)))};
}

function roundedCommitEdge(parent,child,slot) {
  const a=parent,b=child;
  if(a.y===b.y)return `M${a.x} ${a.y}H${b.x}`;
  const sign=Math.sign(b.y-a.y),radius=7;
  const elbow=slot===0?a.x+19:b.x-19;
  return `M${a.x} ${a.y}H${elbow-radius}Q${elbow} ${a.y} ${elbow} ${a.y+sign*radius}V${b.y-sign*radius}Q${elbow} ${b.y} ${elbow+radius} ${b.y}H${b.x}`;
}
if(typeof module!=='undefined')module.exports={commitLayout,roundedCommitEdge};
