/* Search and commands are presentation only. No command bypasses a business form. */
window.UICommands = (() => {
  const L=UI.L;
  let current=null, generation=0;
  const pages=[
    ['give','Work','Công việc','list-todo'],['work/issues','Issues','Sự cố','circle-alert'],
    ['work/approvals','Approvals','Phê duyệt','circle-check'],['departments','Organization','Tổ chức','network'],
    ['processes/workflows','Processes','Quy trình','workflow'],['library/skills','Library','Thư viện','library'],
    ['business/projects','Business records','Dữ liệu nghiệp vụ','database'],['operations/models','Operations','Vận hành','activity'],
    ['settings/appearance','Settings','Cài đặt','settings']
  ];
  function inputContext(target) {return target.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"])');}
  function commands() {
    const options=pages.map(([path,en,vi,icon])=>({id:'page-'+path,label:L(en,vi),detail:L('Open page','Mở trang'),icon,run:()=>go('#/'+path)}));
    options.push({id:'new',label:L('New task','Giao việc mới'),detail:L('Open the assignment form','Mở biểu mẫu giao việc'),icon:'plus',run:async()=>{
      go('#/give');await route();$('newTaskBtn').click();
    }});
    for(const theme of ['light','dark','auto']) options.push({id:'theme-'+theme,label:L('Display: ','Hiển thị: ')+({light:L('Light','Sáng'),dark:L('Dark','Tối'),auto:L('System','Hệ thống')}[theme]),icon:'sliders-horizontal',run:()=>UIPreferences.save({...UIPreferences.get(),theme})});
    for(const p of UIPalettes.items) options.push({id:'palette-'+p.id,label:L('Palette: ','Màu nhấn: ')+L(p.en,p.vi),icon:'sliders-horizontal',run:()=>UIPreferences.save({...UIPreferences.get(),palette:p.id})});
    for(const density of ['comfortable','compact']) options.push({id:'density-'+density,label:L('Spacing: ','Mật độ: ')+(density==='compact'?L('Compact','Gọn'):L('Comfortable','Thoáng')),icon:'sliders-horizontal',run:()=>UIPreferences.save({...UIPreferences.get(),density})});
    for(const language of ['vi','en']) options.push({id:'lang-'+language,label:L('Language: ','Ngôn ngữ: ')+(language==='vi'?'Tiếng Việt':'English'),icon:'settings',run:()=>{langSet(language);route();}});
    options.push({id:'help',label:L('Keyboard shortcuts','Phím tắt'),icon:'circle-help',run:help});
    return options;
  }
  function open() {
    if(current && !current.element.open) {current.element.addEventListener("close",open,{once:true});return;}
    if(current?.element.open) {current.element.querySelector('input').focus();return;}
    if(document.querySelector('dialog[open]')) return;
    document.querySelectorAll('.ui-popover:popover-open').forEach(p=>p.hidePopover());
    const mine=++generation;
    let tasks=[], error='', loading=true, visible=[], selected=0;
    const modal=UI.dialog({title:L('Find or run a command','Tìm kiếm hoặc chọn lệnh'),description:L('Search tasks by title, ID or owner. Commands change this workspace on your device.','Tìm công việc theo tên, mã hoặc người giữ. Các lệnh giao diện áp dụng trên thiết bị này.'),content:`<label class="ui-field"><span>${L('Search tasks and commands','Tìm công việc và lệnh')}</span><input id="commandInput" class="ui-input" role="combobox" aria-autocomplete="list" aria-expanded="true" aria-controls="commandResults" autocomplete="off" placeholder="${L('Page, task, person, appearance…','Trang, công việc, người giữ, giao diện…')}"></label><div class="ui-command-results" id="commandResults" role="listbox" aria-label="${L('Results','Kết quả')}"></div><p class="ui-muted" id="commandStatus" role="status" aria-live="polite"></p>`});
    current=modal;
    modal.element.classList.add('ui-command-dialog');
    const input=modal.element.querySelector('input'),host=modal.element.querySelector('#commandResults'),status=modal.element.querySelector('#commandStatus');
    const norm=value=>String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/đ/g,'d').toLocaleLowerCase();
    function highlight() {
      [...host.children].forEach((el,i)=>el.setAttribute('aria-selected',String(i===selected)));
      if(visible.length) input.setAttribute('aria-activedescendant','command-option-'+selected);
      else input.removeAttribute('aria-activedescendant');
    }
    function draw() {
      const query=norm(input.value.trim());
      visible=commands().filter(o=>norm(o.label+' '+(o.detail || '')).includes(query));
      const matches=tasks.filter(t=>!query || norm([t.title,t.id,t.owner_name].join(' ')).includes(query));
      visible.push(...matches.slice(0,query?40:8).map(t=>({id:t.id,label:t.title || t.id,detail:[t.owner_name,t.id].filter(Boolean).join(' / '),icon:'list-todo',run:()=>go('#/give/'+encodeURIComponent(t.id))})));
      selected=Math.min(selected,Math.max(0,visible.length-1));
      host.innerHTML=visible.map((o,i)=>`<div role="option" id="command-option-${i}" aria-selected="${i===selected}" data-command-index="${i}">${UIIcon(o.icon)}<span><strong>${esc(o.label)}</strong>${o.detail?`<small>${esc(o.detail)}</small>`:''}</span></div>`).join('');
      highlight();
      status.textContent=error || (loading?L('Loading tasks… Commands are ready.','Đang tải công việc… Có thể chọn lệnh.') : L(`${num(visible.length)} results. Use arrow keys and Enter.`,`${num(visible.length)} kết quả. Dùng phím mũi tên và Enter.`));
      if(!visible.length && !loading && !error) status.textContent=L('No results. Try a task ID, owner or page name.','Không có kết quả. Thử mã công việc, người giữ hoặc tên trang.');
    }
    function choose(index) {
      const option=visible[index];if(!option)return;
      // Run after the shared dialog restores focus so a new page/form can own it.
      modal.element.addEventListener('close',()=>{Promise.resolve(option.run()).catch(err=>toast(err.message,true));},{once:true});
      modal.close();
    }
    input.addEventListener('input',()=>{selected=0;draw();});
    input.addEventListener('keydown',e=>{
      if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)) {
        e.preventDefault();
        selected=e.key==='Home'?0:e.key==='End'?Math.max(0,visible.length-1):Math.max(0,Math.min(visible.length-1,selected+(e.key==='ArrowDown'?1:-1)));
        highlight();host.children[selected]?.scrollIntoView({block:'nearest'});
      } else if(e.key==='Enter') {e.preventDefault();choose(selected);}
    });
    host.addEventListener('click',e=>{const option=e.target.closest('[data-command-index]');if(option)choose(Number(option.dataset.commandIndex));});
    modal.element.addEventListener('close',()=>{if(current===modal)current=null;generation++;},{once:true});
    draw();input.focus();
    loadRegister().then(data=>{if(generation!==mine || !modal.element.open)return;tasks=data.items;loading=false;draw();}).catch(err=>{if(generation!==mine || !modal.element.open)return;loading=false;error=L('Tasks could not load: ','Chưa tải được công việc: ')+err.message;draw();});
  }
  function help() {
    if(document.querySelector('dialog[open]'))return;
    const keys=[['Ctrl / ⌘ K',L('Search and commands','Tìm kiếm và lệnh')],['/',L('Search tasks on Work','Tìm công việc tại trang Công việc')],['N',L('New task on Work','Giao việc mới tại trang Công việc')],['J / K',L('Next / previous task','Công việc tiếp theo / trước đó')],['Enter',L('Open focused task or command','Mở công việc hoặc lệnh đang chọn')],['Esc',L('Close the top overlay, then go back','Đóng hộp đang mở, rồi quay lại')],['?',L('Keyboard shortcuts','Trợ giúp phím tắt')]];
    UI.dialog({title:L('Keyboard shortcuts','Phím tắt'),description:L('Single-key shortcuts pause while typing. Work shortcuts apply to the task list.','Phím đơn tạm dừng khi nhập liệu. Phím Công việc áp dụng tại danh sách công việc.'),content:`<dl class="ui-shortcut-list">${keys.map(([key,label])=>`<div><dt>${UI.kbd(key)}</dt><dd>${esc(label)}</dd></div>`).join('')}</dl>`});
  }
  document.addEventListener('keydown',e=>{
    if(e.isComposing || inputContext(e.target))return;
    if((e.ctrlKey || e.metaKey) && e.key.toLowerCase()==='k') {e.preventDefault();open();}
    else if(!e.ctrlKey && !e.metaKey && !e.altKey && e.key==='?') {e.preventDefault();help();}
  });
  if($('commandBtn')) $('commandBtn').onclick=open;if($('shortcutBtn')) $('shortcutBtn').onclick=help;
  const refresh=()=>{
    $('commandBtn').setAttribute('aria-label',L('Search tasks and commands (Ctrl or Command K)','Tìm công việc và lệnh (Ctrl hoặc Command K)'));
    $('commandBtn').title=L('Search tasks and commands','Tìm công việc và lệnh');
    $('shortcutBtn').setAttribute('aria-label',L('Keyboard shortcuts (?)','Phím tắt (?)'));$('shortcutBtn').title=L('Keyboard shortcuts','Phím tắt');
  };
  refresh();
  return {open,help,refresh};
})();

window.UIMotion = (()=>{
  let entered=false;
  try {entered=sessionStorage.getItem('onx-work-intro-v1')==='true';}catch{}
  function enter() {
    if(entered || !document.querySelector('#giveList .work-row'))return;
    entered=true;try {sessionStorage.setItem('onx-work-intro-v1','true');}catch{}
    if(UIPreferences.get().motion==='reduced' || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)return;
    const easing='cubic-bezier(0.2,0,0,1)';
    const start=performance.now();
    const counters=[...$('workStats').querySelectorAll('strong')].map(el=>({el,text:el.textContent,value:Number(el.textContent.replace(/[^0-9]/g,''))}));
    const frame=now=>{
      const fraction=Math.min(1,(now-start)/480);
      const reduced=UIPreferences.get().motion==='reduced' || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
      counters.forEach(({el,text,value})=>{el.textContent=fraction===1 || reduced?text:num(Math.round(value*(1-(1-fraction)**3)));});
      if(fraction<1 && !reduced)requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
    [...$('workStats').children].forEach((el,i)=>el.animate?.([{opacity:0,transform:'translateY(4px)'},{opacity:1,transform:'none'}],{duration:200,delay:i*24,easing}));
    [...$('giveList').children].slice(0,12).forEach((el,i)=>el.animate?.([{opacity:0,transform:'translateY(4px)'},{opacity:1,transform:'none'}],{duration:200,delay:i*16,easing}));
  }
  const stop=()=>{if(UIPreferences.get().motion==='reduced' || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)
    document.getAnimations().filter(a=>a.effect?.getKeyframes().some(k=>k.transform && k.transform!=='none')).forEach(a=>a.cancel());};
  window.matchMedia?.('(prefers-reduced-motion: reduce)').addEventListener('change',stop);
  return {enter,stop};
})();
