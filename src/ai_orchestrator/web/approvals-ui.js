/* Decision presentation. API action, payload and server authority remain in decide(). */
window.ApprovalsUI = (() => {
  const L = UI.L;
  let request = 0, snapshot = null, detailRecord = null;
  const active = () => parseHash().name === 'approval' || (parseHash().name === 'work' && parseHash().arg === 'approvals');
  const live = a => a.actionable !== false && !a.expired;
  const actionButton = (a, action, label, variant='secondary') => UI.button({label,variant,size:'sm'}).replace('<button ',`<button data-do="${action}" data-id="${esc(a.approval_id || a.id)}" `);
  function actions(a) {
    return actionButton(a,'approve',tr('appr.approve','Approve'),'primary') + actionButton(a,'ask',L('Ask for information','Hỏi thêm thông tin')) + actionButton(a,'reject',tr('appr.reject','Reject'),'danger');
  }
  function render() {
    if (!snapshot) return;
    const all = snapshot.items, waiting = all.filter(live), showAll = state.approvalFilter === 'all';
    const shown = showAll ? all : waiting;
    $('needStats').innerHTML = UI.metrics({label:L('Approval queue','Danh sách phê duyệt'),items:[
      {key:'pending',label:L('Needs a decision','Cần quyết định'),value:waiting.length,selected:!showAll,emphasized:true,note:L('Requests you can answer','Yêu cầu còn có thể trả lời')},
      {key:'all',label:L('Everything pending','Tất cả đang chờ'),value:all.length,selected:showAll,note:L('Including expired requests','Bao gồm yêu cầu hết hạn')}]});
    $('workSub').textContent=L(`${num(shown.length)} requests`,`${num(shown.length)} yêu cầu`);
    $('workFilter').querySelectorAll('[data-f]').forEach(b=>{b.classList.toggle('on',b.dataset.f===(showAll?'all':'pending'));b.setAttribute('aria-pressed',String(b.dataset.f===(showAll?'all':'pending')));});
    $('workList').setAttribute('aria-label',L('Approval requests','Yêu cầu phê duyệt'));$('workFilter').setAttribute('aria-label',L('Approval filters','Bộ lọc phê duyệt'));
    $('workList').innerHTML=shown.map(a=>`<div class="ui-list-row ui-detail-row" role="listitem" data-approval="${esc(a.approval_id)}"><div class="ui-row-main"><h3><a href="#/approval/${encodeURIComponent(a.approval_id)}">${esc(a.task_title || a.action_type)}</a></h3><div class="ui-metadata"><span>${esc(a.action_type)}</span>${UI.copyId(a.approval_id)}${UI.badge(a.effect_class || '—')}${a.risk_level==='high'?UI.badge(L('High risk','Rủi ro cao'),'warning'):''}</div><dl class="ui-record-fields"><div><dt>${L('Requested by','Người yêu cầu')}</dt><dd>${esc(a.requested_by || L('Unknown','Chưa rõ'))}</dd></div><div><dt>${L('Assigned reviewer','Người xét duyệt')}</dt><dd>${esc(a.assigned_approver_id || L('Approval queue','Danh sách phê duyệt'))}</dd></div><div><dt>${L('Waiting','Đã chờ')}</dt><dd>${esc(a.waiting_days ?? '—')} ${L('days','ngày')}</dd></div></dl></div><div class="ui-row-actions">${live(a)?actions(a):UI.badge(L('Expired','Hết hạn'),'neutral')+`<p class="ui-muted">${L('A new request is required.','Cần tạo lại yêu cầu.')}</p>`}</div></div>`).join('');
    $('workEmpty').hidden=!!shown.length;
    $('workEmpty').innerHTML=UI.empty({title:L('No decisions waiting','Không có quyết định đang chờ'),description:showAll?L('There are no pending requests in this queue.','Danh sách không có yêu cầu đang chờ.'):L('Expired requests remain under Everything pending.','Yêu cầu hết hạn vẫn hiển thị trong Tất cả đang chờ.')});
    $('navApprovalCount').textContent=String(snapshot.count);$('navApprovalCount').hidden=!snapshot.count;
    $('needStats').querySelectorAll('[data-ui-metric]').forEach(b=>b.onclick=()=>{state.approvalFilter=b.dataset.uiMetric;render();});
  }
  async function load() {
    const mine=++request;
    $('view-work').dataset.page='approvals';$('view-work').classList.add('ui-page');
    $('needStats').classList.add('ui-metrics-host');$('issuesCard').hidden=true;$('approvalsCard').hidden=false;$('workNote').hidden=true;
    $('needsHeading').textContent=tr('need.approvals','Approvals');
    $('workList').setAttribute('aria-busy','true');$('workList').innerHTML='';$('approvalLoading').hidden=false;$('approvalLoading').innerHTML=UI.skeleton(L('Loading requests','Đang tải yêu cầu'));$('workEmpty').hidden=true;$('approvalError').hidden=true;
    // A previous workspace/request must never be shown as the new queue's metrics.
    $('needStats').innerHTML=UI.skeleton(L('Loading approval counts','Đang tải số yêu cầu'));
    try {
      const inbox=await apiGet('/approvals/inbox');
      if(mine!==request || !active()) return;
      state.inbox=inbox.items;snapshot=inbox;render();
    } catch(err) {
      if(mine!==request || !active()) return;
      $('workList').innerHTML='';$('needStats').innerHTML='';
      $('approvalError').hidden=false;
      $('approvalError').innerHTML=UI.callout({title:L('Could not load requests','Chưa tải được yêu cầu'),description:err.message,status:'danger',actions:UI.button({label:L('Retry loading','Tải lại danh sách'),action:'reload-approvals'})});
      $('approvalError').querySelector('button').onclick=load;
    } finally {if(mine===request){$('workList').setAttribute('aria-busy','false');$('approvalLoading').hidden=true;}}
  }
  async function detail(id) {
    const approval=await apiGet(`/approvals/${encodeURIComponent(id)}`);
    if(parseHash().name!=='approval' || parseHash().arg!==id)return;
    detailRecord=approval;
    const answerable=approval.status==='pending' && (!approval.expires_at || Date.parse(approval.expires_at)>Date.now());
    UI.drawer({title:approval.action_type,description:L('Review the requested action and its exact draft before deciding.','Xem hành động được yêu cầu và bản dự thảo trước khi quyết định.'),content:`<div class="ui-metadata">${UI.copyId(approval.id)}${UI.badge(approval.status)}</div>${approval.reason?UI.callout({title:L('Reason for review','Lý do cần duyệt'),description:approval.reason,status:'info'}):''}${approval.action_type==='agent.provision'?`<a class="ui-btn ui-btn-secondary ui-btn-md" href="#/processes/provision/${encodeURIComponent(approval.action_payload?.draft_id || '')}">${L('Edit the plan before approval','Chỉnh sửa kế hoạch trước khi duyệt')}</a>`:''}${UI.panel({title:L('Exact draft','Bản dự thảo'),content:valueHTML(approval.action_payload || {})})}${approval.task_id?`<a class="ui-btn ui-btn-ghost ui-btn-md" href="#/give/${encodeURIComponent(approval.task_id)}/decisions">${L('Open task','Mở công việc')}</a>`:''}<div class="ui-toolbar">${answerable?actions(approval):UI.badge(L('Cannot answer this request','Yêu cầu không còn nhận quyết định'))}</div>`});
  }
  function confirm(id, action) {
    const a=state.inbox.find(a=>a.approval_id===id) || (detailRecord?.id===id ? detailRecord : null);
    const title=action==='approve'?L('Approve request','Chấp thuận yêu cầu'):action==='reject'?L('Reject request','Từ chối yêu cầu'):L('Ask for information','Hỏi thêm thông tin');
    const context=a?.task_title || a?.action_type || id;
    UI.dialog({title,description:context,content:`${UI.copyId(id)}${action==='reject'?UI.textarea({label:L('Reason for rejection','Lý do từ chối'),name:'approval-note',required:true}):''}${action==='ask'?UI.textarea({label:L('What needs clarification?','Cần làm rõ điều gì?'),name:'approval-question',required:true}):UI.callout({title:L('Decision will be recorded','Quyết định sẽ được ghi nhận'),description:action==='approve'?L('The agent may continue after the server validates this approval.','Agent có thể tiếp tục sau khi hệ thống kiểm tra phê duyệt.'):L('This request will be rejected. Review the draft before confirming.','Yêu cầu sẽ bị từ chối. Xem bản dự thảo trước khi xác nhận.'),status:action==='approve'?'info':'warning'})}`,confirmLabel:title,onConfirm:async dialog=>{
      let extra={};
      if(action==='ask') {const field=dialog.querySelector('textarea');if(!field.value.trim()){field.focus();throw new Error(L('Enter a question before sending.','Nhập câu hỏi trước khi gửi.'));}extra={needs_information:true,note:field.value.trim()};}
      if(action==='reject') {const field=dialog.querySelector('textarea');if(!field.value.trim()){field.focus();throw new Error(L('Enter a reason before rejecting.','Nhập lý do trước khi từ chối.'));}extra={note:field.value.trim()};}
      await decide(id,action==='ask'?'request-information':action,extra,true);
    }});
  }
  return {load,detail,confirm};
})();
