/* Shell presentation. Only the current tenant is offered; no business writes. */
window.UIShell = (() => {
  const L = UI.L;
  const mobile = window.matchMedia?.('(max-width: 760px)');
  const sidebar = $('sidebar'), drawer = $('sidebarDrawer');
  const workspaceHome = document.createComment('workspace popover home');
  $('workspacePanel').before(workspaceHome);
  const anchor = document.createComment('sidebar home');
  sidebar.before(anchor);
  const tooltips = new Map();
  sidebar.querySelectorAll('[data-nav]').forEach((link,i)=>{
    const host=document.createElement('span'); host.className='shell-nav-item';
    link.before(host); host.append(link);
    const tip=document.createElement('span'); tip.className='ui-tooltip'; tip.id=`shell-nav-tip-${i}`; tip.setAttribute('role','tooltip'); host.append(tip);
    tooltips.set(link,tip);
  });
  let restoreTo = null;
  let previousOverflow = '', organization = null, organizationError = false;
  function indicator() {
    const collapsed=document.documentElement.classList.contains('sidebar-collapsed') && !drawer.open;
    tooltips.forEach((tip,link)=>{
      link.parentElement.classList.toggle('ui-tooltip-host',collapsed);
      tip.textContent=link.querySelector('[data-i18n]')?.textContent || link.textContent;
      if(collapsed) link.setAttribute('aria-describedby',tip.id); else link.removeAttribute('aria-describedby');
    });
    const selected = sidebar.querySelector('[aria-current="page"]');
    const bar = $('navIndicator');
    bar.hidden = !selected;
    if (!selected) return;
    const nav = sidebar.getBoundingClientRect(), row = selected.getBoundingClientRect();
    bar.style.transform = `translateY(${row.top - nav.top + sidebar.scrollTop}px)`;
    bar.style.height = `${row.height}px`;
  }
  function workspace() {
    const name = organization?.name || L('Current workspace', 'Không gian hiện tại');
    $('workspaceName').textContent = name;
    $('workspaceBtn').title = L('Switch workspace: ', 'Chọn không gian: ') + name;
    $('workspaceBtn').setAttribute('aria-label', $('workspaceBtn').title);
    $('workspacePanel').innerHTML = `<h2 id="workspaceTitle">${L('Workspace', 'Không gian làm việc')}</h2>
      <p class="ui-muted">${L('The workspace available in this session.', 'Không gian có thể truy cập trong phiên này.')}</p>
      <button class="ui-btn ui-btn-secondary ui-btn-md shell-workspace-current" type="button" aria-current="true">${UIIcon('check')}<span>${esc(name)}<small>${esc(ORG || L('No workspace ID', 'Chưa có ID không gian'))}</small></span></button>
      ${organizationError ? `<p role="status" class="ui-muted">${L('Could not load the workspace name. The current ID is shown.', 'Chưa tải được tên không gian. ID hiện tại được hiển thị.')}</p>` : ''}
      <a class="ui-btn ui-btn-ghost ui-btn-sm" href="#/settings/organization">${L('Organization settings', 'Cài đặt tổ chức')}</a>`;
    $('workspacePanel').querySelector('button').onclick = () => $('workspacePanel').hidePopover();
    $('workspacePanel').querySelector('a').onclick = () => { $('workspacePanel').hidePopover(); if (drawer.open) { restoreTo=$('mainContent'); drawer.close(); } };
  }
  function appearance() {
    const prefs = UIPreferences.get();
    $('appearancePanel').innerHTML = `<h2 id="appearanceTitle">${L('Appearance', 'Giao diện')}</h2>
      <p class="ui-muted">${L('Saved on this device. Preview changes immediately.', 'Lưu trên thiết bị này. Thay đổi được xem trước ngay.')}</p>
      <fieldset><legend>${L('Color mode', 'Chế độ màu')}</legend>${UI.tabs({label:L('Color mode','Chế độ màu'),segmented:true,selected:['auto','light','dark'].indexOf(prefs.theme),items:[{label:L('System','Hệ thống')},{label:L('Light','Sáng')},{label:L('Dark','Tối')}]})}</fieldset>
      <fieldset><legend>${L('Accent color', 'Màu nhấn')}</legend><div class="shell-palettes">${UIPalettes.items.map(p=>`<button class="shell-palette" type="button" data-shell-palette="${p.id}" aria-pressed="${p.id===prefs.palette}"><span class="shell-swatch" style="--swatch:${p[document.documentElement.dataset.colorMode || 'light'][0]}" aria-hidden="true"></span>${esc(p[state.lang])}${p.id===prefs.palette?UIIcon('check'):''}</button>`).join('')}</div></fieldset>
      <fieldset><legend>${L('Density', 'Mật độ')}</legend>${UI.tabs({label:L('Density','Mật độ'),segmented:true,selected:prefs.density==='compact'?1:0,items:[{label:L('Comfortable','Thoải mái')},{label:L('Compact','Gọn')}]})}</fieldset>
      <button class="ui-btn ui-btn-ghost ui-btn-sm" type="button" popovertarget="appearancePanel" popovertargetaction="hide">${L('Close', 'Đóng')}</button>`;
    const groups = $('appearancePanel').querySelectorAll('.ui-tabs');
    groups.forEach((group,i)=>group.querySelectorAll('button').forEach((button,j)=>{
      button.dataset.shellPref = i===0?'theme':'density';
      button.dataset.shellValue = (i===0?['auto','light','dark']:['comfortable','compact'])[j];
    }));
    UI.hydrate($('appearancePanel'));
  }
  function bell(n) {
    $('notifBtn').dataset.unread = String(n>0);
    $('notifBtn').setAttribute('aria-label', L('Notifications', 'Thông báo') + (n ? ` · ${n} ${L('unread','chưa đọc')}` : ''));
    $('notifBtn').title = $('notifBtn').getAttribute('aria-label');
  }
  function refresh() {
    $('langBtn').textContent='VI'; $('langEnBtn').textContent='EN';
    document.querySelectorAll('[data-shell-lang]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.shellLang===state.lang)));
    $('languageGroup').setAttribute('aria-label',L('Language','Ngôn ngữ'));
    $('backLabel').textContent=L('Back','Quay lại');
    $('appearanceBtn').setAttribute('aria-label',L('Appearance','Giao diện'));
    $('appearanceBtn').title=L('Appearance','Giao diện');
    $('mobileNavBtn').setAttribute('aria-label',L('Open navigation','Mở điều hướng'));
    $('mobileNavClose').setAttribute('aria-label',L('Close navigation','Đóng điều hướng'));
    sidebar.setAttribute('aria-label',L('Sections','Các mục'));
    drawer.setAttribute('aria-label',L('Navigation','Điều hướng'));
    $('path').setAttribute('aria-label',L('Breadcrumb','Đường dẫn'));
    $('authRecommendation').textContent=L('Enable service-token authentication before sharing access. This session does not represent an authenticated user.', 'Bật xác thực bằng service token trước khi chia sẻ quyền truy cập. Phiên này không đại diện cho người dùng đã đăng nhập.');
    workspace(); appearance(); bell(unreadCount()); UI.hydrate($('languageGroup')); indicator();
  }
  function closeBell(restore=false) {
    $('notifPanel').hidden=true; $('notifBtn').setAttribute('aria-expanded','false');
    if(restore) $('notifBtn').focus();
  }
  function openDrawer() {
    if (!mobile?.matches || drawer.open) return;
    closeBell();
    restoreTo=$('mobileNavBtn');
    $('sidebarDrawerBody').append(sidebar);
    drawer.append($('workspacePanel'));
    previousOverflow=document.body.style.overflow; document.body.style.overflow='hidden';
    drawer.showModal(); $('mobileNavBtn').setAttribute('aria-expanded','true');
    requestAnimationFrame(indicator);
  }
  drawer.addEventListener('close',()=>{
    $('workspacePanel').hidePopover();
    anchor.after(sidebar); workspaceHome.after($('workspacePanel')); document.body.style.overflow=previousOverflow;
    $('mobileNavBtn').setAttribute('aria-expanded','false');
    (mobile?.matches ? restoreTo : $('sidebarToggle'))?.focus(); indicator();
  });
  $('mobileNavBtn').onclick=openDrawer; $('mobileNavClose').onclick=()=>drawer.close();
  drawer.addEventListener('click',e=>{if(e.target===drawer) drawer.close();});
  sidebar.addEventListener('click',e=>{if(e.target.closest('[data-nav]') && drawer.open) { restoreTo=$('mainContent'); drawer.close(); }});
  $('navItems').addEventListener('scroll',indicator);
  window.addEventListener('resize',indicator);
  mobile?.addEventListener('change',()=>{if(drawer.open) drawer.close(); indicator();});
  if(window.ResizeObserver) new ResizeObserver(indicator).observe(sidebar);
  document.querySelectorAll('[data-shell-lang]').forEach(b=>b.onclick=()=>{langSet(b.dataset.shellLang); route();});
  $('appearancePanel').addEventListener('click',e=>{
    const b=e.target.closest('[data-shell-pref],[data-shell-palette]'); if(!b) return;
    const key=b.dataset.shellPalette?'palette':b.dataset.shellPref;
    const value=b.dataset.shellPalette || b.dataset.shellValue;
    UIPreferences.save({...UIPreferences.get(),[key]:value});
    appearance();
    $('appearancePanel').querySelector(key==='palette'?`[data-shell-palette="${value}"]`:`[data-shell-pref="${key}"][data-shell-value="${value}"]`)?.focus();
    indicator();
  });
  // The shared helper selects/focuses; shell controls also apply the selected value.
  document.addEventListener('keydown',e=>{
    if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key) && e.target.closest?.('#languageGroup,#appearancePanel .ui-tabs'))
      document.activeElement?.click();
  });
  for(const id of ['appearancePanel','workspacePanel']) $(id).addEventListener('toggle',e=>{
    $(id==='appearancePanel'?'appearanceBtn':'workspaceBtn').setAttribute('aria-expanded',String(e.newState==='open'));
    if(e.newState==='open') { closeBell(); if(id==='appearancePanel') appearance(); requestAnimationFrame(()=>UI.hydrate($(id))); }
  });
  $('notifBtn').addEventListener('click',()=>{
    if(!$('notifPanel').hidden) $('notifPanel').querySelector('button,a')?.focus();
  });
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape') {
      if(!$('notifPanel').hidden) { e.preventDefault(); e.stopImmediatePropagation(); closeBell(true); }
      else if(drawer.open) { e.preventDefault(); e.stopImmediatePropagation(); drawer.close(); }
    }
    if(e.key==='Tab' && drawer.open) {
      const buttons=[...drawer.querySelectorAll('button,a,summary')].filter(n=>n.getClientRects().length && !n.disabled);
      const first=buttons[0], last=buttons.at(-1);
      if(e.shiftKey && document.activeElement===first) {e.preventDefault();last.focus();}
      else if(!e.shiftKey && document.activeElement===last) {e.preventDefault();first.focus();}
    }
  },true);
  const result={refresh,indicator,bell};
  // Defer until window.UIShell is assigned; langSet then keeps the segmented labels.
  queueMicrotask(()=>{
    refresh();
    if(ORG_RE.test(ORG)) apiGet(`/organizations/${ORG}`).then(data=>{organization=data;workspace();}).catch(()=>{organizationError=true;workspace();});
  });
  return result;
})();
