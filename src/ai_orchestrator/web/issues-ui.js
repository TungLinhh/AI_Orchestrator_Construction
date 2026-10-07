/* Issues presentation; existing register membership and retry handler remain authoritative. */
window.IssuesUI = (() => {
  const L = UI.L, rows = new Map(), batch = 100;
  let snapshot = null, limit = batch, queryKey = '', request = 0;
  const member = task => ['failed','blocked','canceled','cancelled'].includes(task.status) ||
    (task.status === 'created' && task.waiting_on === 'you');
  const active = () => parseHash().name === 'work' && parseHash().arg === 'issues';
  const tone = status => status === 'failed' || status === 'blocked' ? 'danger' : status === 'created' ? 'warning' : 'neutral';
  const labels = () => [L('All','Tất cả'),L('Failed','Thất bại'),L('Blocked','Bị chặn'),L('Needs attention','Chờ xử lý')];
  function loading(value) {
    $('issueLoading').hidden = !value;
    $('issueList').setAttribute('aria-busy',String(value));
    if(value) {$('issueLoading').innerHTML=UI.skeleton(L('Loading issues','Đang tải sự cố'));$('issueEmpty').hidden=true;}
    $('refreshIssues').disabled=value;
  }
  function error(err) {
    $('issueError').hidden=false;
    $('issueError').innerHTML=UI.callout({title:L('Could not refresh issues','Chưa tải được sự cố'),
      description:err.message,status:'danger',actions:UI.button({label:L('Retry loading','Tải lại danh sách'),action:'reload-issues'})});
  }
  async function load() {
    const mine=++request;
    $('view-work').dataset.page='issues';$('view-work').classList.add('ui-page');
    $('issuesCard').hidden=false;$('approvalsCard').hidden=true;$('workNote').hidden=true;
    $('needStats').classList.add('ui-metrics-host');
    $('needsHeading').textContent=tr('need.issues','Issues');
    loading(true);$('issueError').hidden=true;
    try {
      const q=await loadRegister();
      if(mine!==request || !active()) return;
      render(q);
      const count=q.items.filter(member).length;
      $('navIssueCount').textContent=String(count);$('navIssueCount').hidden=!count;
    } catch(err) {if(mine===request && active()) error(err);}
    finally {if(mine===request) loading(false);}
  }
  function render(q) {
    snapshot=q;
    $('issuesCard').querySelector('h2').textContent=L('Issue register','Danh sách sự cố');
    const all=(q.items || []).filter(member), counts=[all.length,
      all.filter(t=>t.status==='failed').length,all.filter(t=>t.status==='blocked').length,
      all.filter(t=>t.status==='created').length];
    const filters=['','failed','blocked','waiting'], names=labels();
    if(active()) {
      $('needStats').innerHTML=UI.metrics({label:L('Issue filters','Bộ lọc sự cố'),items:filters.map((key,i)=>({
        key,label:i===0?L('Needs review','Cần xem'):names[i],value:counts[i],selected:issueFilter===key,emphasized:i===0,
        note:[L('Issues on the register','Sự cố trong danh sách'),L('Work could not finish','Công việc chưa hoàn thành'),
          L('Work stopped by a blocker','Công việc gặp trở ngại'),L('New tasks waiting on you','Task mới đang chờ bạn')][i]}))});
    }
    $('issueSearchLabel').textContent=L('Search issues','Tìm sự cố');
    $('issueSearch').setAttribute('aria-label',L('Search title, owner, ID or error','Tìm theo tên, người giữ, mã hoặc lỗi'));
    $('issueSearch').placeholder=L('Title, owner, ID or error…','Tên, người giữ, mã hoặc lỗi…');
    $('clearIssueSearch').textContent=L('Clear search','Xóa tìm kiếm');
    $('refreshIssues').textContent=L('Refresh','Tải lại');
    $('issueList').setAttribute('aria-label',tr('need.issues','Issues'));
    $('issueFilter').setAttribute('aria-label',L('Filter issues','Lọc sự cố'));
    $('issueFilter').querySelectorAll('button[data-i]').forEach((b,i)=>{
      b.textContent=`${names[i]} · ${num(counts[i])}`;b.setAttribute('aria-pressed',String(b.dataset.i===issueFilter));
    });
    const query=$('issueSearch').value.trim().toLocaleLowerCase(state.lang);
    const key=JSON.stringify([query,issueFilter]);if(queryKey!==key){queryKey=key;limit=batch;}
    const found=all.filter(t=>(!issueFilter || t.status===(issueFilter==='waiting'?'created':issueFilter)) &&
      (!query || [t.title,t.owner_name,t.id,t.last_error].some(value=>String(value || '').toLocaleLowerCase(state.lang).includes(query))));
    const visible=found.slice(0,limit), host=$('issueList'), wanted=new Set();
    const markup=t=>`<div class="ui-row-main"><h3><a href="#/give/${encodeURIComponent(t.id)}/log" title="${esc(t.title)}">${esc(t.title || t.id)}</a></h3>
      <div class="ui-metadata">${UI.copyId(t.id)}<span>${esc(t.task_type || '—')}</span><span>${esc(t.owner_name || L('No owner','Chưa có người giữ'))}</span>${t.children?`<span>${num(t.children)} ${L('subtasks','việc con')}</span>`:''}</div>
      <details class="ui-disclosure" data-tone="${tone(t.status)}"><summary>${UIIcon('chevron-down')}<span>${esc(t.last_error ? String(t.last_error).split('\n')[0] : L('No reason recorded','Chưa ghi nhận lý do'))}</span></summary>
        <div class="ui-disclosure-body">${t.last_error?UI.logViewer({label:L('Full error','Lỗi đầy đủ'),text:String(t.last_error),filter:false,copyLabel:L('Copy full error','Sao chép toàn bộ lỗi')}):UI.callout({title:L('No reason recorded','Chưa ghi nhận lý do'),description:L('Open the task log to review its recorded steps.','Mở nhật ký công việc để xem các bước đã ghi nhận.'),status:'info'})}
        <div class="ui-toolbar"><a class="ui-btn ui-btn-ghost ui-btn-sm" href="#/give/${encodeURIComponent(t.id)}/log">${L('Open task log','Mở nhật ký')}</a><button class="ui-btn ui-btn-ghost ui-btn-sm" data-retry="${esc(t.id)}">${L('Retry as a new task','Thử lại bằng task mới')}</button></div>
        <p class="ui-muted">${L('Retry creates a new task. The server checks workflow rules and permission.','Thử lại tạo task mới. Hệ thống kiểm tra quyền và quy tắc workflow trước khi chạy.')}</p></div></details></div>
      <div class="ui-row-actions">${UI.badge(statusName(t.status),tone(t.status))}</div>`;
    if(typeof document.createDocumentFragment==='function') {
      visible.forEach((t,i)=>{
        let row=rows.get(t.id);
        if(!row){row={node:document.createElement('div')};row.node.className='ui-list-row ui-detail-row';row.node.dataset.task=t.id;row.node.setAttribute('role','listitem');rows.set(t.id,row);}
        const signature=JSON.stringify([t,state.lang]);
        if(row.signature!==signature){const opened=row.node.querySelector('details')?.open;
          const focused=row.node.contains(document.activeElement);row.node.innerHTML=markup(t);row.signature=signature;
          if(opened)row.node.querySelector('details').open=true;
          if(focused)row.node.querySelector('a').focus({preventScroll:true});}
        wanted.add(row.node);if(host.children[i]!==row.node)host.insertBefore(row.node,host.children[i] || null);
      });
      [...host.children].forEach(node=>{if(!wanted.has(node))node.remove();});
      const valid=new Set(all.map(t=>t.id));for(const id of rows.keys())if(!valid.has(id))rows.delete(id);
    } else host.innerHTML=visible.map(t=>`<div class="ui-list-row ui-detail-row" role="listitem" data-task="${esc(t.id)}">${markup(t)}</div>`).join('');
    $('issueSub').textContent=`${num(found.length)} / ${num(all.length)}`;
    $('issueVisibleCount').textContent=L(`Showing ${num(visible.length)} of ${num(found.length)} issues`,`Đang hiển thị ${num(visible.length)} / ${num(found.length)} sự cố`);
    $('loadMoreIssues').hidden=visible.length>=found.length;$('loadMoreIssues').textContent=L('Load 100 more','Tải thêm 100');
    $('clearIssueSearch').hidden=!query;$('issueEmpty').hidden=!!found.length;host.hidden=!found.length;
    if(!found.length)$('issueEmpty').innerHTML=UI.empty({kind:all.length?'no-results':'empty',
      title:all.length?L('No matching issues','Không có sự cố phù hợp'):L('No issues on the register','Chưa có sự cố trong danh sách'),
      description:all.length?L('Clear the search or filters to see the other issues.','Xóa tìm kiếm hoặc bộ lọc để xem các sự cố khác.'):L('The register has no tasks matching the issue categories.','Danh sách hiện không có task thuộc các nhóm sự cố.'),
      actions:UI.button({label:all.length?L('Clear filters','Xóa bộ lọc'):L('View work','Xem công việc'),action:all.length?'clear-issue-filters':'view-issue-work'})});
    if((q.items || []).length<(q.total || 0))error(new Error(L('Some tasks could not be loaded. Refresh to complete the list.','Chưa tải đủ task. Tải lại để nhận danh sách đầy đủ.')));
    UI.hydrate($('issuesCard'));
  }
  function filter(value) {issueFilter=value;limit=batch;if(snapshot)render(snapshot);else load();}
  let searchTimer;
  $('issueSearch').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{if(snapshot)render(snapshot);},100);});
  $('clearIssueSearch').onclick=()=>{$('issueSearch').value='';if(snapshot)render(snapshot);$('issueSearch').focus();};
  $('refreshIssues').onclick=load;
  $('loadMoreIssues').onclick=()=>{const count=$('issueList').children.length;limit+=batch;render(snapshot);
    if($('loadMoreIssues').hidden)$('issueList').children[count]?.querySelector('a')?.focus({preventScroll:true});};
  $('needStats').addEventListener('click',e=>{if(active()){const b=e.target.closest('[data-ui-metric]');if(b)filter(b.dataset.uiMetric);}});
  document.addEventListener('click',e=>{const action=e.target.closest('[data-ui-action]')?.dataset.uiAction;
    if(action==='reload-issues')load();if(action==='view-issue-work')go('#/give');if(action==='clear-issue-filters'){$('issueSearch').value='';filter('');}});
  document.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key) && e.target.closest?.('#issueFilter'))document.activeElement?.click();});
  return {load,render,filter};
})();
