/* Work presentation: bounded initial DOM, stable keyed rows, no business writes. */
window.WorkUI = (() => {
  const L = UI.L, batch = 100;
  const rows = new Map();
  let snapshot = null, limit = batch, viewKey = '', selected = null, savedScroll = null;
  let returning = false, searchTimer;
  const relativeFormatters={vi:new Intl.RelativeTimeFormat('vi',{numeric:'auto'}),en:new Intl.RelativeTimeFormat('en',{numeric:'auto'})};
  const nf = value => new Intl.NumberFormat(state.lang === 'vi' ? 'vi-VN' : 'en-GB').format(value);
  const tone = status => ({completed:'success',failed:'danger',blocked:'danger',expired:'danger',
    canceled:'neutral',cancelled:'neutral',running:'info',assigned:'info',created:'neutral',
    waiting_for_approval:'warning',waiting_for_input:'warning',pending:'warning'}[status] || 'neutral');
  function relative(iso) {
    const seconds=(Date.parse(iso)-Date.now())/1000;
    if(!Number.isFinite(seconds)) return '—';
    const unit=Math.abs(seconds)<3600?'minute':Math.abs(seconds)<86400?'hour':'day';
    return relativeFormatters[state.lang].format(Math.round(seconds/({minute:60,hour:3600,day:86400}[unit])),unit);
  }
  function refresh() {
    $('newTaskLabel').textContent=L('New task','Giao việc mới');
    $('runBtn').textContent=L('Assign and run','Giao việc và chạy');
    $('goalHelp').textContent=L('Describe the result, relevant context and acceptance criteria.', 'Nêu kết quả cần đạt, bối cảnh và tiêu chí chấp nhận.');
    $('goal').setAttribute('aria-label',tr('give.what','What should the company work on?'));
    $('taskSearch').setAttribute('aria-label',L('Search tasks','Tìm công việc'));
    $('clearTaskSearch').setAttribute('aria-label',L('Clear search','Xóa tìm kiếm'));
    $('giveFilter').setAttribute('aria-label',L('Filter tasks','Lọc công việc'));
    $('workStats').setAttribute('aria-label',L('Work filters','Bộ lọc công việc'));
    $('taskSortLabel').textContent=L('Sort','Sắp xếp');
    [...$('taskSort').options].forEach((o,i)=>o.textContent=[L('Newest first','Mới nhất'),L('Oldest first','Cũ nhất'),L('Title','Tiêu đề'),L('Needs attention first','Chờ bạn trước')][i]);
    $('loadMoreTasks').textContent=L('Load 100 more','Tải thêm 100 công việc');
    $('logSearch').setAttribute('aria-label',L('Search this page of events','Tìm sự kiện trên trang này'));
    $('logKind').setAttribute('aria-label',L('Filter event types','Lọc loại sự kiện'));
    sticky();
  }
  function sticky() {
    const height=$('crumbs').getBoundingClientRect().height || 56;
    document.documentElement.style.setProperty('--work-toolbar-top',`${height}px`);
    UI.hydrate($('workToolbar'));
    const active=$('giveFilter').querySelector('[aria-pressed="true"]');
    const bar=$('giveFilter').querySelector('.ui-tab-indicator');
    if(active && bar) {bar.style.top=`${active.offsetTop}px`;bar.style.height=`${active.offsetHeight}px`;}
  }
  function filter(value) {
    giveFilter=value;
    limit=batch;
    renderGive(null,false);
  }
  function stats(q) {
    if(!$('workStats').children.length) $('workStats').innerHTML=['you','in_flight','settled',''].map(value=>
      `<button class="work-stat" type="button" data-work-filter="${value}" aria-pressed="false"><span class="work-stat-label"></span><strong></strong><span class="work-stat-help"></span></button>`).join('');
    const values=[q.needs_you,q.in_flight,q.settled,q.total];
    const labels=[L('Waiting on you','Chờ bạn'),L('With an agent','Agent đang làm'),L('Settled','Đã xong'),L('Total','Tổng')];
    const hints=[L('Decision or owner needed','Cần quyết định hoặc người giữ'),L('Running or assigned','Đang chạy hoặc đã giao'),L('All terminal tasks, including failures','Gồm mọi task kết thúc, cả thất bại'),L('Tasks on the register','Công việc trong danh sách')];
    if(typeof document.createDocumentFragment!=='function') {
      $('workStats').innerHTML=['you','in_flight','settled',''].map((value,i)=>
        `<button class="work-stat" type="button" data-work-filter="${value}" aria-pressed="${value===giveFilter}"><span class="work-stat-label">${labels[i]}</span><strong>${values[i]==null?'—':nf(values[i])}</strong><span class="work-stat-help">${hints[i]}</span></button>`).join('');
    }
    [...$('workStats').children].forEach((button,i)=>{
      button.querySelector('.work-stat-label').textContent=labels[i];
      button.querySelector('strong').textContent=values[i]==null?'—':nf(values[i]);
      button.querySelector('.work-stat-help').textContent=hints[i];
      button.setAttribute('aria-pressed',String(button.dataset.workFilter===giveFilter));
      button.dataset.zero=String(values[i]===0);
    });
    const counts=[q.total,q.needs_you,q.in_flight,q.settled];
    $('giveFilter').querySelectorAll('button').forEach((button,i)=>{
      button.textContent=[L('All','Tất cả'),...labels.slice(0,3)][i]+` · ${counts[i]==null?'—':nf(counts[i])}`;
      button.setAttribute('aria-pressed',String(button.dataset.w===giveFilter));
    });
  }
  function rowMarkup(task) {
    const wait=task.waiting_on==='you'?L('Needs you','Chờ bạn'):task.waiting_on==='an_agent'?L('With an agent','Agent đang làm'):L('Settled','Đã kết thúc');
    return `<div class="work-row-main"><div class="work-row-title"><a class="work-open" data-work-role="open" href="#/give/${encodeURIComponent(task.id)}" title="${esc(task.title)}">${esc(task.title || task.id)}</a>${UI.badge(statusName(task.status),tone(task.status))}</div>
      <div class="work-row-meta">${UI.copyId(task.id)}<span class="work-type">${esc(task.task_type || '—')}</span>${task.children?`<span>${UIIcon('network')}${nf(task.children)} ${L('subtasks','việc con')}</span>`:''}${task.delegated?`<span>${esc(task.delegated)}</span>`:''}<time datetime="${esc(task.created_at || '')}" title="${esc(task.created_at || '')}">${esc(relative(task.created_at))}</time></div>
      ${task.last_error?`<details class="work-row-error"><summary>${UIIcon('circle-alert')}<span>${esc(String(task.last_error).split('\n')[0])}</span></summary><div class="work-error-full"><pre><code>${esc(task.last_error)}</code></pre>${UI.button({label:L('Copy error','Sao chép lỗi'),variant:'ghost',size:'sm',icon:'copy'}).replace('<button ','<button data-ui-copy="'+esc(task.last_error)+'" ')}</div></details>`:''}</div>
      <div class="work-row-owner">${task.owner_name?UI.avatar(task.owner_name):UIIcon('circle-help')}<span>${esc(task.owner_name || L('No owner','Chưa có người giữ'))}</span></div>
      <div class="work-row-wait">${UI.badge(wait,task.waiting_on==='you'?'warning':'neutral')}${task.approvals_waiting?`<a href="#/give/${encodeURIComponent(task.id)}/decisions">${nf(task.approvals_waiting)} ${L('decisions','cần duyệt')}</a>`:''}</div>`;
  }
  function list(q) {
    snapshot=q; refresh(); stats(q);
    $('registerError').hidden=true;
    const all=q.items || [], query=$('taskSearch').value.trim().toLocaleLowerCase(state.lang);
    const sort=$('taskSort').value;
    const key=JSON.stringify([giveFilter,query,sort]);
    if(key!==viewKey) {limit=batch;viewKey=key;}
    let filtered=all.filter(task=>(!giveFilter || task.waiting_on===({you:'you',in_flight:'an_agent',settled:'nobody'}[giveFilter])) &&
      (!query || [task.title,task.owner_name,task.id].some(v=>String(v || '').toLocaleLowerCase(state.lang).includes(query))));
    filtered.sort((a,b)=>{
      if(sort==='title') return String(a.title).localeCompare(String(b.title),state.lang);
      if(sort==='attention') {const delta=Number(b.waiting_on==='you')-Number(a.waiting_on==='you');if(delta) return delta;}
      const delta=(Date.parse(b.created_at)||0)-(Date.parse(a.created_at)||0);
      return (sort==='oldest'?-delta:delta) || String(a.id).localeCompare(String(b.id));
    });
    const visible=filtered.slice(0,limit), host=$('giveList');
    host.setAttribute('role','list');host.setAttribute('aria-label',L('Tasks','Công việc'));
    // Keep existing nodes and disclosure/focus state when an unchanged row is polled.
    if(typeof document.createDocumentFragment==='function') {
      const wanted=new Set();
      visible.forEach((task,index)=>{
        let item=rows.get(task.id);
        if(!item) {const node=document.createElement('div');node.className='row ui-list-row work-row';node.dataset.task=task.id;node.setAttribute('role','listitem');item={node};rows.set(task.id,item);}
        const signature=JSON.stringify([task,state.lang]);
        if(item.signature!==signature) {
          const opened=item.node.querySelector('details')?.open;
          const hadFocus=item.node.contains(document.activeElement);
          item.node.innerHTML=rowMarkup(task);item.signature=signature;
          if(opened && item.node.querySelector('details')) item.node.querySelector('details').open=true;
          if(hadFocus) item.node.querySelector('.work-open').focus({preventScroll:true});
        } else {
          const time=item.node.querySelector('time'), text=relative(task.created_at);
          if(time.textContent!==text) time.textContent=text;
        }
        item.node.dataset.selected=String(task.id===selected);
        wanted.add(item.node);
        if(host.children[index]!==item.node) host.insertBefore(item.node,host.children[index] || null);
      });
      [...host.children].forEach(node=>{if(!wanted.has(node)) node.remove();});
      const valid=new Set(all.map(task=>task.id));for(const id of rows.keys()) if(!valid.has(id)) rows.delete(id);
    } else {
      // Non-layout DOM environments retain the same bounded, escaped markup.
      host.innerHTML=visible.map(task=>`<div role="listitem" class="row ui-list-row work-row" data-task="${esc(task.id)}">${rowMarkup(task)}</div>`).join('');
    }
    $('registerCount').textContent=`${nf(filtered.length)} / ${q.total==null?'—':nf(q.total)}`;
    $('visibleTaskCount').textContent=L(`Showing ${nf(visible.length)} of ${nf(filtered.length)} results`, `Đang hiển thị ${nf(visible.length)} / ${nf(filtered.length)} kết quả`);
    $('loadMoreTasks').hidden=visible.length>=filtered.length;
    $('clearTaskSearch').hidden=!query;
    $('giveEmpty').hidden=filtered.length>0;
    host.hidden=!filtered.length;
    if(!filtered.length) $('giveEmpty').innerHTML=UI.empty({
      title:all.length?L('No matching tasks','Không có công việc phù hợp'):L('No tasks yet','Chưa có công việc nào'),
      description:all.length?L('Clear the search or filters to see the register.','Xóa tìm kiếm hoặc bộ lọc để xem danh sách.'):L('Give the company its first task.','Giao công việc đầu tiên cho tổ chức.'),
      kind:all.length?'no-results':'empty',actions:UI.button({label:all.length?L('Clear filters','Xóa bộ lọc'):L('New task','Giao việc mới'),action:all.length?'clear-work-filters':'new-work-task'})});
    if(all.length<(q.total||0)) error(new Error(L('The register is incomplete. Retry loading the missing tasks.','Danh sách chưa tải đủ. Tải lại để nhận các công việc còn thiếu.')));
    sticky();
    if(returning && savedScroll!==null) {
      returning=false;
      requestAnimationFrame(()=>{if(parseHash().name==='give' && !parseHash().arg) {window.scrollTo(0,savedScroll);rows.get(selected)?.node.querySelector('.work-open')?.focus({preventScroll:true});}});
    }
  }
  function loading(value) {
    $('registerLoading').hidden=!value;
    if(value) {$('registerLoading').innerHTML=UI.skeleton(L('Loading tasks','Đang tải công việc'));$('giveEmpty').hidden=true;}
    $('giveList').setAttribute('aria-busy',String(value));
  }
  function error(err) {
    loading(false);$('registerError').hidden=false;
    $('registerError').innerHTML=UI.callout({title:L('Could not refresh tasks','Chưa tải được danh sách'),description:err.message,status:'danger',actions:UI.button({label:L('Retry','Tải lại'),action:'retry-work-list'})});
  }
  function detailLoading(value) {
    $('taskDetailLoading').hidden=!value;
    if(value) {$('taskDetailLoading').innerHTML=UI.skeleton(L('Loading task','Đang tải công việc'));$('taskDetailError').hidden=true;}
  }
  function detailError(err) {
    detailLoading(false);$('taskDetailError').hidden=false;
    if(!taskReport) $('taskTitle').textContent=L('Task unavailable','Không tải được công việc');
    $('taskDetailError').innerHTML=UI.callout({title:L('Could not load this task','Chưa tải được công việc'),description:err.message,status:'danger',actions:UI.button({label:L('Retry','Tải lại'),action:'retry-work-detail'})});
  }
  function taskStats(task,s) {
    $('taskDetailError').hidden=true;
    $('taskStats').innerHTML=`<dl class="work-summary"><div><dt>${L('Status','Trạng thái')}</dt><dd>${UI.badge(statusName(task.status),tone(task.status))}</dd></div>`+
      [[L('Delegated to','Đã giao cho'),s.delegated_to], [L('Executed','Đã chạy'),s.executed], [L('In flight','Đang chạy'),s.in_flight], [L('Pending decisions','Chờ duyệt'),s.approvals_pending]].map(([label,value])=>`<div><dt>${label}</dt><dd>${value==null?'—':nf(value)}</dd></div>`).join('')+'</dl>';
  }
  function openForm() {$('newTaskForm').open=true;$('goal').focus();$('newTaskForm').scrollIntoView?.({block:'nearest',behavior:'smooth'});}
  $('newTaskBtn').onclick=openForm;
  $('newTaskForm').addEventListener('toggle',()=>$('newTaskBtn').setAttribute('aria-expanded',String($('newTaskForm').open)));
  $('taskSearch').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>renderGive(null,false),100);};
  $('clearTaskSearch').onclick=()=>{$('taskSearch').value='';renderGive(null,false);$('taskSearch').focus();};
  $('taskSort').onchange=()=>renderGive(null,false);
  $('loadMoreTasks').onclick=()=>{
    const before=$('giveList').children.length;
    limit+=batch;if(snapshot) list(snapshot);
    if($('loadMoreTasks').hidden) $('giveList').children[before]?.querySelector('.work-open')?.focus({preventScroll:true});
  };
  $('workStats').addEventListener('click',event=>{const button=event.target.closest('[data-work-filter]');if(button) filter(button.dataset.workFilter);});
  $('giveList').addEventListener('click',event=>{const link=event.target.closest('a.work-open');if(link) {selected=link.closest('[data-task]').dataset.task;savedScroll=window.scrollY;}});
  window.addEventListener('hashchange',()=>{if(parseHash().name==='give' && !parseHash().arg) returning=true;});
  window.addEventListener('resize',sticky);
  if(window.ResizeObserver) new ResizeObserver(sticky).observe($('crumbs'));
  document.addEventListener('click',event=>{
    const action=event.target.closest('[data-ui-action]')?.dataset.uiAction;
    if(action==='clear-work-filters') {$('taskSearch').value='';filter('');}
    if(action==='new-work-task') openForm();
    if(action==='retry-work-list') renderGive();
    if(action==='retry-work-detail') renderOneTask(parseHash().arg);
  });
  document.addEventListener('keydown',event=>{
    if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key) && event.target.closest?.('#giveFilter')) {document.activeElement?.click();return;}
    if(event.ctrlKey || event.metaKey || event.altKey || event.target.closest?.('input,textarea,select,[contenteditable="true"]') || document.querySelector('dialog[open],.ui-popover:popover-open')) return;
    if(parseHash().name!=='give' || parseHash().arg) return;
    if(event.key==='/') {event.preventDefault();$('taskSearch').focus();}
    if(event.key.toLowerCase()==='n') {event.preventDefault();openForm();}
    if(['j','k'].includes(event.key)) {
      event.preventDefault();
      const links=[...$('giveList').querySelectorAll('.work-open')];
      const current=links.indexOf(document.activeElement), next=Math.max(0,Math.min(links.length-1,current+(event.key==='j'?1:-1)));
      links[next]?.focus();links[next]?.scrollIntoView({block:'nearest'});
      if(event.key==='j' && current===links.length-1 && !$('loadMoreTasks').hidden) {const i=links.length;$('loadMoreTasks').click();$('giveList').querySelectorAll('.work-open')[i]?.focus();}
    }
  });
  // Wrap the existing authorized create/run handler; validation and loading only.
  const run=$('runBtn').onclick;
  $('runBtn').onclick=async()=>{
    const goal=$('goal');$('formError').hidden=true;
    if(!goal.value.trim()) {goal.setAttribute('aria-invalid','true');$('goalError').textContent=L('Describe the task before assigning it.','Nhập yêu cầu trước khi giao việc.');$('goalError').hidden=false;goal.focus();return;}
    goal.removeAttribute('aria-invalid');$('goalError').hidden=true;
    $('runBtn').setAttribute('aria-busy','true');
    try {await run();} finally {$('runBtn').removeAttribute('aria-busy');$('runBtn').textContent=L('Assign and run','Giao việc và chạy');}
  };
  $('goal').addEventListener('input',()=>{if($('goal').value.trim()) {$('goal').removeAttribute('aria-invalid');$('goalError').hidden=true;}});
  refresh();
  return {list,loading,error,detailLoading,detailError,taskStats,filter,refresh};
})();
