/* Source choice is explicit. Uploaded and mock files share the same API/parser. */
window.SourceUI = (() => {
  const chosen = new Set();
  let library = [];
  let pendingTask = null;
  let libraryOffset = 0;
  let libraryTotalCount = 0;
  const libraryTotal = () => libraryTotalCount;
  /* Source revision drafts stay separate from ordinary creation and resume.
     revisionDraft holds only the original task id, the workflow, the
     idempotency key and the payload signature. create() and the
     missing-source resume path never read it, so a pending revision cannot
     leak into a new task. revisionChosen and revisionMode() are the only
     revision selection state; ordinary creation/resume use chosen/mode()
     exclusively, so abandoning a revision never alters an unsaved draft. */
  let revisionDraft = null;
  const revisionChosen = new Set();
  const revisionMode = () => document.querySelector('input[name="revisionChoice"]:checked')?.value;
  const TERMINAL_SOURCE_STATUS = new Set(['completed', 'failed', 'canceled', 'cancelled', 'expired']);
  const revisionKey = () => (window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : 'rev-' + Date.now().toString(36) + '-' + Math.floor(Math.random() * 1e9).toString(36));
  const revisionNotice = (text, bad = false) => {
    const el = $('sourceRevisionNotice');
    if (!el) return;
    el.textContent = text;
    el.className = bad ? 'ui-help ui-error' : 'ui-help';
  };
  const L = (en, vi) => UI.L(en, vi);
  const mode = () => document.querySelector('input[name="sourceChoice"]:checked')?.value;
  const notice = (text, bad = false) => {
    $('sourceNotice').textContent = text;
    $('sourceNotice').className = bad ? 'ui-help ui-error' : 'ui-help';
  };
  async function binary(path, options) {
    const response = await fetch(API + path, {...options, headers: authHeaders(options?.headers)});
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      if (response.status === 401) { forgetToken(); askToken(''); }
      throw new Error(error.error?.message || JSON.stringify(error.detail || response.statusText));
    }
    return response;
  }
  async function download(path, filename) {
    const response = await binary(path);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = filename; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function paint() {
    $('sourceLibrary').innerHTML = library.map(file => `<div class="ui-panel-body">
      <label class="ui-field"><span><input type="checkbox" data-source-id="${esc(file.id)}" ${chosen.has(file.id) ? 'checked' : ''} ${file.state !== 'ready' ? 'disabled' : ''}> ${esc(file.filename)} ${file.synthetic ? '· MOCK' : ''}</span></label>
      <div class="ui-help">${esc(file.state)} · ${num(file.byte_count)} bytes · ${esc(file.classification)}${file.error ? ' · ' + esc(file.error) : ''}</div>
      <div class="btn-row"><button type="button" class="ui-btn ui-btn-ghost ui-btn-sm" data-source-preview="${esc(file.id)}">${L('Preview / review', 'Xem / kiểm tra')}</button><button type="button" class="ui-btn ui-btn-ghost ui-btn-sm" data-source-download="${esc(file.id)}">${L('Original file', 'File gốc')}</button></div>
    </div>`).join('') || `<p class="ui-help">${L('No source files yet.', 'Chưa có nguồn. Hãy upload hoặc tạo file mock.')}</p>`;
    $('sourceSelectionCount').textContent = L('Selected: ', 'Đã chọn: ') + chosen.size;
    $('sourceLibrary').querySelectorAll('[data-source-id]').forEach(input => input.onchange = () => {
      if (input.checked) chosen.add(input.dataset.sourceId); else chosen.delete(input.dataset.sourceId);
      $('sourceSelectionCount').textContent = L('Selected: ', 'Đã chọn: ') + chosen.size;
    });
    $('sourceLibrary').querySelectorAll('[data-source-preview]').forEach(button => button.onclick = async () => {
      try {
        const file = await apiGet(`/source-files/${encodeURIComponent(button.dataset.sourcePreview)}`);
        openSheet(file.filename, `<p>${esc((file.warnings || []).join(' · '))}</p><pre class="reason">${esc((file.extraction.segments || []).map(s => `${s.locator}\n${s.text}`).join('\n\n'))}</pre>${file.state === 'needs_review' && file.extraction.segments.length ? `<label class="ui-field"><span>${L('Confirm only after checking the original file. Explain your review:', 'Chỉ xác nhận sau khi kiểm tra file gốc. Ghi kết quả kiểm tra:')}</span><textarea class="ui-input" id="sourceReviewNote" minlength="10"></textarea></label><button class="ui-btn ui-btn-primary ui-btn-md" id="sourceConfirm">${L('Save reviewed version', 'Lưu phiên bản đã kiểm tra')}</button>` : ''}`);
        if ($('sourceConfirm')) $('sourceConfirm').onclick = async () => {
          try {
            const reviewed = await apiPost(`/source-files/${encodeURIComponent(file.id)}/confirm`, {note: $('sourceReviewNote').value});
            chosen.add(reviewed.id); $('sheet').close(); await refresh();
          } catch (error) { toast(error.message, true); }
        };
      } catch (error) { notice(error.message, true); }
    });
    $('sourceLibrary').querySelectorAll('[data-source-download]').forEach(button => button.onclick = () => {
      const file = library.find(f => f.id === button.dataset.sourceDownload);
      download(`/source-files/${encodeURIComponent(file.id)}/download`, file.filename).catch(error => notice(error.message, true));
    });
  }
  async function refresh(append = false) {
    if (!append) {library = []; libraryOffset = 0;}
    notice(L('Loading sources…', 'Đang tải kho nguồn…'));
    try {
      const page = await apiGet('/source-files', {limit: 50, offset: libraryOffset});
      library.push(...page.items); libraryOffset += page.items.length;
      libraryTotalCount = page.total;
      $('sourceMore').hidden = libraryOffset >= page.total;
      const revMore = $('sourceRevisionMore');
      if (revMore) revMore.hidden = libraryOffset >= page.total;
      paint(); notice(L('Select only files this task may use.', 'Chỉ chọn những file được phép dùng cho công việc này.'));
    } catch (error) { notice(error.message, true); throw error; }
  }
  const isSourceWorkflow = () => ['document_review', 'quote_comparison'].includes($('sourceWorkflow').value);
  function assertOrdinarySources() {
    if (chosen.size || $('sourceUploadFiles').files.length || mode()) {
      throw new Error(L('Selected files are not attached to ordinary tasks. Choose document review or quote comparison, or clear the source selection to continue without files.', 'Nguồn đã chọn chưa được gắn vào công việc thông thường. Chọn rà hồ sơ hoặc so sánh báo giá, hoặc bỏ chọn nguồn để tiếp tục không kèm file.'));
    }
  }
  async function generate() {
    if (!isSourceWorkflow()) throw new Error(L('Choose document review or quote comparison before generating mock files.', 'Chọn rà hồ sơ hoặc so sánh báo giá trước khi tạo file mock.'));
    if (mode() !== 'mock') throw new Error(L('Choose mock sources first.', 'Hãy chọn tạo file mock trước.'));
    const result = await apiPost('/source-files/mock?workflow=' + encodeURIComponent($('sourceWorkflow').value));
    chosen.clear(); result.items.forEach(file => chosen.add(file.id));
    await refresh(); notice(L('Actual mock files generated and extracted.', 'Đã tạo và đọc file mock qua parser.'));
  }
  async function create(body) {
    if (!isSourceWorkflow()) throw new Error(L('Choose a document workflow first.', 'Chọn cách xử lý hồ sơ trước.'));
    if (!mode()) throw new Error(L('Choose source files or generate mock files before running.', 'Bạn muốn chọn nguồn dữ liệu hay tạo file mock để kiểm thử?'));
    if (mode() === 'mock' && !chosen.size) await generate();
    const ids = [...chosen];
    if (mode() === 'mock' && ids.some(id => !library.find(f => f.id === id)?.synthetic)) {
      throw new Error(L('Mock mode only accepts generated mock files.', 'Chế độ mock chỉ nhận file mock được tạo trong hệ thống.'));
    }
    return apiPost('/source-workflows', {...body, workflow: $('sourceWorkflow').value, selection: {mode: mode(), file_ids: ids}});
  }
  async function task(id) {
    /* A revision form belongs to exactly one original task. Drop a stale one
       before rendering, so it can never leak into another task's view.
       Clear ONLY revision state; ordinary chosen/mode and pendingTask stay. */
    const stale = $('sourceRevisionWrap');
    if (stale && stale.dataset.original !== id) {
      stale.remove();
      revisionDraft = null;
      revisionChosen.clear();
    }
    try {
      const [source, artifacts] = await Promise.all([apiGet(`/tasks/${encodeURIComponent(id)}/sources`), apiGet(`/tasks/${encodeURIComponent(id)}/artifacts`)]);
      if (parseHash().arg !== id) return;
      const panel = $('sourceTaskPanel');
      panel.hidden = !source.manifest && !artifacts.items.length;
      if (panel.hidden) {
        const detail = await apiGet(`/tasks/${encodeURIComponent(id)}`);
        if (parseHash().arg !== id) return;
        if (!detail.input?.source_workflow) return;
        /* Terminal root source tasks are revision-eligible even without an old
           manifest (backend accepts previous_manifest_id null). Offer revision
           there; reserve attach/resume for active waiting input only. */
        const terminalEligible = ['document_review', 'quote_comparison'].includes(detail.input?.source_workflow)
          && TERMINAL_SOURCE_STATUS.has(detail.status)
          && !detail.parent_task_id
          && !detail.input?.business_workflow;
        if (terminalEligible) {
          panel.hidden = false;
          $('sourceTaskFiles').innerHTML = `<p class="ui-help">${L('No prior manifest on this terminal task. Choose replacement sources to create a revision draft; the original stays unchanged.', 'Task kết thúc này chưa có manifest. Chọn nguồn thay thế để tạo bản nháp; task gốc giữ nguyên.')}</p>`;
          if ($('sourceDownloads')) $('sourceDownloads').innerHTML = '';
          await revisionSection(id, source);
          return;
        }
        panel.hidden = false;
        $('sourceTaskFiles').innerHTML = `<p>${esc(detail.last_error || L('This workflow needs sources. Select files in the new-task form, then attach them here.', 'Luồng đang cần nguồn. Chọn file ở form giao việc, rồi gắn vào task tại đây.'))}</p><button class="ui-btn ui-btn-secondary ui-btn-md" id="sourcePickForTask">${L('Choose sources for this task', 'Chọn nguồn cho task này')}</button><button class="ui-btn ui-btn-secondary ui-btn-md" id="sourceAttach">${L('Attach selected files', 'Gắn các file đang chọn')}</button>`;
        $('sourcePickForTask').onclick = () => {
          pendingTask = id; $('sourceWorkflow').value = detail.input.source_workflow; window.location.hash = '#/give';
          $('newTaskForm').open = true; $('sourceResume').hidden = false;
          $('sourceHeading').scrollIntoView({behavior: 'smooth'});
          notice(L('Choose or upload files, then resume the existing task.', 'Chọn hoặc upload file rồi tiếp tục task đang chờ.'));
        };
        $('sourceAttach').onclick = async () => {
          try {
            if (!mode()) throw new Error(L('Choose source or mock mode first.', 'Cần chọn nguồn hoặc mock trước.'));
            if (mode() === 'mock' && !chosen.size) await generate();
            await apiPost(`/tasks/${encodeURIComponent(id)}/sources`, {mode: mode(), file_ids: [...chosen]});
            await apiPost(`/tasks/${encodeURIComponent(id)}/run`, {}); await renderOneTask(id);
          } catch (error) { toast(error.message, true); }
        };
        return;
      }
      $('sourceTaskFiles').innerHTML = (source.manifest?.sources || []).map(file => `<p>${esc(file.filename)} ${file.synthetic ? '· MOCK' : ''}<br><code>${esc(file.sha256)}</code></p>`).join('') + `<p class="ui-help">${L('Manifest SHA-256: ', 'Manifest SHA-256: ')}${esc(source.manifest?.sha256 || '')}</p>`;
      $('sourceDownloads').innerHTML = artifacts.items.map(file => `<button class="ui-btn ui-btn-secondary ui-btn-md" data-artifact="${esc(file.id)}">${L('Download ', 'Tải ')}${esc(file.format.toUpperCase())}</button>`).join('') || esc(artifacts.reason || L('Waiting for a validated result.', 'Đang chờ kết quả qua kiểm chứng.'));
      $('sourceDownloads').querySelectorAll('[data-artifact]').forEach(button => button.onclick = () => {
        const file = artifacts.items.find(f => f.id === button.dataset.artifact);
        download(`/source-artifacts/${encodeURIComponent(file.id)}/download`, file.filename).catch(error => toast(error.message, true));
      });
      await revisionSection(id, source);
    } catch (error) {
      if (parseHash().arg === id) { $('sourceTaskPanel').hidden = false; $('sourceTaskFiles').textContent = error.message; }
    }
  }
  function paintRevisionFiles() {
    const host = $('revisionFileList');
    if (!host) return;
    host.innerHTML = library.map(file => `<label class="ui-field"><span><input type="checkbox" data-revision-source-id="${esc(file.id)}" ${revisionChosen.has(file.id) ? 'checked' : ''} ${file.state !== 'ready' ? 'disabled' : ''}> ${esc(file.filename)} ${file.synthetic ? '· MOCK' : ''}</span></label><div class="ui-help">${esc(file.state)} · ${num(file.byte_count)} bytes</div>`).join('') || `<p class="ui-help">${L('No source files yet. Generate mock files or upload first.', 'Chưa có file nguồn. Hãy tạo file mock hoặc upload trước.')}</p>`;
    host.querySelectorAll('[data-revision-source-id]').forEach(input => input.onchange = () => {
      if (input.checked) revisionChosen.add(input.dataset.revisionSourceId); else revisionChosen.delete(input.dataset.revisionSourceId);
      updateRevisionCount();
    });
    updateRevisionCount();
  }
  function updateRevisionCount() {
    /* Revision count only. Never mirror into the ordinary picker. */
    const text = L('Selected: ', 'Đã chọn: ') + revisionChosen.size;
    if ($('revisionSelectionCount')) $('revisionSelectionCount').textContent = text;
  }
  async function generateRevision() {
    if (revisionMode() !== 'mock') throw new Error(L('Choose replacement mock files first.', 'Hãy chọn tạo file mock thay thế trước.'));
    const workflow = (revisionDraft && revisionDraft.workflow) || 'document_review';
    const result = await apiPost('/source-files/mock?workflow=' + encodeURIComponent(workflow));
    revisionChosen.clear(); result.items.forEach(file => revisionChosen.add(file.id));
    await refresh(); paintRevisionFiles();
    revisionNotice(L('Actual mock files generated and extracted.', 'Đã tạo và đọc file mock qua parser.'));
  }
  async function uploadRevision() {
    if (revisionMode() !== 'sources') {
      revisionNotice(L('Choose replacement source files first.', 'Hãy chọn file nguồn thay thế trước.'), true);
      return;
    }
    const picker = $('revisionUploadFiles');
    if (!picker || !picker.files.length) {
      revisionNotice(L('Choose files to upload.', 'Chọn file để upload.'), true);
      return;
    }
    const btn = $('sourceRevisionUpload');
    if (btn) btn.disabled = true;
    try {
      const classification = ($('revisionClassification') || {}).value || 'internal';
      const failures = [];
      for (const file of picker.files) {
        revisionNotice(L('Uploading and extracting: ', 'Đang upload và đọc: ') + file.name);
        const form = new FormData();
        form.append('file', file);
        form.append('classification', classification);
        const saved = await (await binary('/source-files', {method: 'POST', body: form})).json();
        if (saved.state === 'ready') revisionChosen.add(saved.id);
        else failures.push(`${saved.filename}: ${saved.state} ${saved.error || ''}`);
      }
      await refresh();
      paintRevisionFiles();
      revisionNotice(failures.length ? failures.join(' · ') : L('Replacement files are ready. Review your selection.', 'Đã đọc file thay thế. Hãy kiểm tra các file đã chọn.'), failures.length > 0);
    } catch (error) {
      revisionNotice(error.message, true);
      throw error;
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  function buildRevisionForm(id, source, workflow) {
    const manifest = source.manifest || {};
    const priorFiles = (manifest.sources || []).map(file => `${esc(file.filename)}${file.synthetic ? ' · MOCK' : ''}`).join(', ');
    const manifestBit = manifest.sha256
      ? ` <code>${esc(manifest.sha256)}</code>${priorFiles ? ' · ' + priorFiles : ''}`
      : ` ${L('(no prior manifest on this task)', '(task này chưa có manifest)')}`;
    $('sourceRevisionForm').innerHTML = `
      <p class="ui-help" id="sourceRevisionOriginal">${L('Original task', 'Task gốc')} <a href="#/give/${esc(id)}">${esc(id)}</a> ${L('stays unchanged; its files keep their history. Prior manifest', 'giữ nguyên; file cũ giữ lịch sử. Manifest trước')}${manifestBit}</p>
      <fieldset><legend>${L('Choose replacement sources explicitly', 'Chọn rõ nguồn thay thế')}</legend>
        <label class="ui-choice" for="revisionChooseSources"><input id="revisionChooseSources" type="radio" name="revisionChoice" value="sources"><span>${L('Replacement source files', 'File nguồn thay thế')}</span></label>
        <label class="ui-choice" for="revisionChooseMock"><input id="revisionChooseMock" type="radio" name="revisionChoice" value="mock"><span>${L('Replacement mock files', 'File mock thay thế')}</span></label>
      </fieldset>
      <div id="revisionFileList" tabindex="0" aria-label="${L('Replacement source files', 'File nguồn thay thế')}"></div>
      <div class="btn-row"><button type="button" class="ui-btn ui-btn-secondary ui-btn-md" id="sourceRevisionMock">${L('Generate replacement mock files', 'Tạo file mock thay thế')}</button><span id="revisionSelectionCount" role="status">Đã chọn: 0</span></div>
      <label class="ui-field"><span>${L('Replacement file upload (TXT, CSV, JSON, DOCX, XLSX, PDF, PPTX, ZIP, images)', 'Upload file thay thế (TXT, CSV, JSON, DOCX, XLSX, PDF, PPTX, ZIP, ảnh)')}</span><input class="ui-input" type="file" id="revisionUploadFiles" multiple></label>
      <label class="ui-field"><span>${L('Replacement classification', 'Phân loại file thay thế')}</span><select class="ui-input" id="revisionClassification"><option value="internal">Internal</option><option value="public">Public</option><option value="confidential">Confidential</option><option value="restricted">Restricted</option><option value="secret">Secret</option></select></label>
      <div class="btn-row"><button type="button" class="ui-btn ui-btn-secondary ui-btn-md" id="sourceRevisionUpload">${L('Upload replacement files', 'Upload file thay thế')}</button><button type="button" class="ui-btn ui-btn-ghost ui-btn-sm" id="sourceRevisionRefresh">${L('Reload sources', 'Tải lại kho')}</button><button type="button" class="ui-btn ui-btn-ghost ui-btn-sm" id="sourceRevisionMore" hidden>${L('Show more sources', 'Xem thêm nguồn')}</button></div>
      <label class="ui-field"><span>${L('Why replace the sources? (10–1000 characters)', 'Vì sao thay nguồn? (10–1000 ký tự)')}</span><textarea class="ui-input" id="sourceRevisionReason" minlength="10" maxlength="1000"></textarea></label>
      <div class="btn-row"><button type="button" class="ui-btn ui-btn-primary ui-btn-md" id="sourceRevisionCreate">${L('Create revision', 'Tạo bản sửa nguồn')}</button><button type="button" class="ui-btn ui-btn-ghost ui-btn-md" id="sourceRevisionCancel">${L('Cancel', 'Hủy')}</button></div>`;
    /* Revision radios stay local: clear ONLY revisionChosen, never touch the
       ordinary sourceChoice picker, chosen set, or pendingTask resume. */
    document.querySelectorAll('input[name="revisionChoice"]').forEach(input => input.onchange = () => {
      if (!input.checked) return;
      revisionChosen.clear();
      paintRevisionFiles();
    });
    const syncMore = () => {
      const more = $('sourceRevisionMore');
      if (more) more.hidden = libraryOffset >= (libraryTotal());
    };
    const refreshRevisionList = async (append = false) => {
      try {
        await refresh(append);
      } catch (error) {
        revisionNotice(error.message, true);
        throw error;
      }
      paintRevisionFiles();
      syncMore();
    };
    $('sourceRevisionMock').onclick = async () => {
      $('sourceRevisionMock').disabled = true;
      try { await generateRevision(); syncMore(); } catch (error) { revisionNotice(error.message, true); } finally { $('sourceRevisionMock').disabled = false; }
    };
    $('sourceRevisionUpload').onclick = async () => {
      try { await uploadRevision(); syncMore(); } catch (error) { /* notice already set */ }
    };
    $('sourceRevisionRefresh').onclick = () => { refreshRevisionList(false).catch(() => {}); };
    $('sourceRevisionMore').onclick = () => { refreshRevisionList(true).catch(() => {}); };
    $('sourceRevisionCancel').onclick = () => {
      /* Cancel clears ONLY revision state; ordinary draft and resume stay. */
      revisionDraft = null;
      revisionChosen.clear();
      $('sourceRevisionForm').hidden = true; $('sourceRevisionForm').innerHTML = '';
      const start = $('sourceRevisionStart');
      if (start) { start.hidden = false; start.focus(); }
      revisionNotice('');
    };
    $('sourceRevisionCreate').onclick = () => createRevision(id, workflow);
    paintRevisionFiles();
  }
  async function createRevision(id, workflow) {
    const btn = $('sourceRevisionCreate');
    if (!revisionDraft || revisionDraft.originalId !== id) {
      revisionNotice(L('The revision draft expired. Start again from Choose replacement sources.', 'Bản nháp đã hết hạn. Hãy bắt đầu lại từ Chọn nguồn thay thế.'), true);
      return;
    }
    const reason = (($('sourceRevisionReason') || {}).value || '').trim();
    if (reason.length < 10 || reason.length > 1000) {
      revisionNotice(L(`Explain the replacement in 10–1000 characters (now ${reason.length}).`, `Hãy ghi lý do thay nguồn 10–1000 ký tự (hiện ${reason.length}).`), true);
      if ($('sourceRevisionReason')) $('sourceRevisionReason').focus();
      return;
    }
    const selected = revisionMode();
    if (!selected) {
      revisionNotice(L('Choose replacement source files or replacement mock files first.', 'Hãy chọn rõ file nguồn thay thế hoặc file mock thay thế trước.'), true);
      return;
    }
    btn.disabled = true;
    revisionNotice(L('Creating revision draft…', 'Đang tạo bản nháp…'));
    try {
      if (selected === 'mock' && !revisionChosen.size) await generateRevision();
      const ids = [...revisionChosen];
      if (!ids.length) throw new Error(L('Select at least one replacement file.', 'Hãy chọn ít nhất một file thay thế.'));
      if (selected === 'mock' && ids.some(fid => !library.find(f => f.id === fid)?.synthetic)) {
        throw new Error(L('Mock mode only accepts generated mock files.', 'Chế độ mock chỉ nhận file mock được tạo trong hệ thống.'));
      }
      /* Same payload retries reuse the key; a changed payload gets a new key. */
      const sig = JSON.stringify({mode: selected, ids: [...ids].sort(), reason});
      if (!revisionDraft.key || revisionDraft.sig !== sig) {
        revisionDraft.key = revisionKey();
        revisionDraft.sig = sig;
      }
      const created = await (await binary(`/tasks/${encodeURIComponent(id)}/source-revisions`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'Idempotency-Key': revisionDraft.key},
        body: JSON.stringify({selection: {mode: selected, file_ids: ids}, reason}),
      })).json();
      if (!created || !created.id) throw new Error(L('The revision did not return a task.', 'Bản sửa không trả về task.'));
      revisionDraft = null;
      revisionChosen.clear();
      const done = $('sourceRevisionWrap');
      if (done) done.remove();
      toast(L('Revision draft created. No run started.', 'Đã tạo bản nháp. Chưa chạy.'));
      window.location.hash = '#/give/' + created.id;
    } catch (error) {
      revisionNotice(error.message, true);
      btn.disabled = false;
    }
  }
  /* Explicit revise action on a terminal root source task, plus the prior-task
     link on a task that is itself a revision. Never runs the new task. */
  async function revisionSection(id, source) {
    const panel = $('sourceTaskPanel');
    const body = panel ? panel.querySelector('.ui-panel-body') : null;
    if (!body) return;
    let wrap = $('sourceRevisionWrap');
    if (wrap && wrap.dataset.original === id) return;
    if (wrap) wrap.remove();
    let detail = null;
    try {
      detail = await apiGet(`/tasks/${encodeURIComponent(id)}`);
    } catch (error) {
      return;
    }
    if (!detail || parseHash().arg !== id) return;
    const input = detail.input || {};
    const prior = input.source_revision;
    const eligible = ['document_review', 'quote_comparison'].includes(input.source_workflow)
      && TERMINAL_SOURCE_STATUS.has(detail.status)
      && !detail.parent_task_id
      && !input.business_workflow;
    if (!prior && !eligible) return;
    wrap = document.createElement('div');
    wrap.id = 'sourceRevisionWrap';
    wrap.dataset.original = id;
    const bits = [];
    if (prior && prior.previous_task_id) {
      /* Factual status wording: never claim Unstarted once the revision has
         settled. detail.status is this revision task's own status. */
      const st = (detail.status || '').toLowerCase();
      let copyEn, copyVi;
      if (st === 'completed') {
        copyEn = 'Completed revision from new sources. No approvals, runs, or outputs were copied; history stays on the original task.';
        copyVi = 'Bản sửa đã hoàn thành từ nguồn mới. Không copy phê duyệt, lần chạy hay kết quả; lịch sử nằm ở task gốc.';
      } else if (st === 'failed') {
        copyEn = 'Failed revision from new sources. No approvals, runs, or outputs were copied; history stays on the original task.';
        copyVi = 'Bản sửa đã thất bại từ nguồn mới. Không copy phê duyệt, lần chạy hay kết quả; lịch sử nằm ở task gốc.';
      } else if (st === 'canceled' || st === 'cancelled' || st === 'expired') {
        copyEn = 'Canceled revision from new sources. No approvals, runs, or outputs were copied; history stays on the original task.';
        copyVi = 'Bản sửa đã hủy từ nguồn mới. Không copy phê duyệt, lần chạy hay kết quả; lịch sử nằm ở task gốc.';
      } else if (st === 'waiting_for_input') {
        copyEn = 'Revision draft waiting for input. No approvals, runs, or outputs were copied; history stays on the original task.';
        copyVi = 'Bản nháp đang chờ nguồn. Không copy phê duyệt, lần chạy hay kết quả; lịch sử nằm ở task gốc.';
      } else {
        copyEn = 'Unstarted draft from new sources. No approvals, runs, or outputs were copied; history stays on the original task.';
        copyVi = 'Bản nháp chưa chạy từ nguồn mới. Không copy phê duyệt, lần chạy hay kết quả; lịch sử nằm ở task gốc.';
      }
      bits.push(`<p class="ui-help" id="sourceRevisionPrior">${L('Revision of', 'Bản sửa của')} <a id="sourceRevisionPriorLink" href="#/give/${esc(prior.previous_task_id)}">${esc(prior.previous_task_id)}</a>${prior.reason ? ' · ' + esc(prior.reason) : ''}</p><p class="ui-help" data-revision-status-copy="${esc(detail.status || '')}">${L(copyEn, copyVi)}</p>`);
    }
    if (eligible) {
      bits.push(`<div class="btn-row"><button type="button" class="ui-btn ui-btn-secondary ui-btn-md" id="sourceRevisionStart">${L('Choose replacement sources', 'Chọn nguồn thay thế')}</button></div><div id="sourceRevisionForm" hidden></div><p class="ui-help" id="sourceRevisionNotice" role="status" aria-live="polite"></p>`);
    }
    wrap.innerHTML = bits.join('');
    body.appendChild(wrap);
    if (!$('sourceRevisionStart')) return;
    $('sourceRevisionStart').onclick = async () => {
      revisionDraft = {originalId: id, workflow: input.source_workflow, key: null, sig: null};
      /* Revision starts with its own empty selection and no default mode.
         Ordinary sourceChoice/chosen, pendingTask resume, and the workflow
         picker are left untouched so a normal draft survives. */
      revisionChosen.clear();
      try {
        await refresh();
      } catch (error) {
        revisionNotice(error.message, true);
      }
      if (!revisionDraft || revisionDraft.originalId !== id || parseHash().arg !== id) return;
      buildRevisionForm(id, source, input.source_workflow);
      $('sourceRevisionForm').hidden = false;
      $('sourceRevisionStart').hidden = true;
      const revMore = $('sourceRevisionMore');
      if (revMore) revMore.hidden = libraryOffset >= libraryTotal();
      const first = $('revisionChooseSources');
      if (first) first.focus();
    };
  }
  function init() {
    $('sourceClearSelection').onclick = () => {
      chosen.clear();
      document.querySelectorAll('input[name="sourceChoice"]').forEach(input => {input.checked = false;});
      $('sourceUploadFiles').value = '';
      paint();
      notice(L('Source selection cleared. No files will be attached to the new task.', 'Đã bỏ chọn nguồn. Công việc mới sẽ không kèm file.'));
    };
    $('sourceResume').onclick = async () => {
      if (!pendingTask) return;
      try {
        if (!mode()) throw new Error(L('Choose source or mock mode first.', 'Cần chọn nguồn hoặc mock trước.'));
        if (mode() === 'mock' && !chosen.size) await generate();
        await apiPost(`/tasks/${encodeURIComponent(pendingTask)}/sources`, {mode: mode(), file_ids: [...chosen]});
        await apiPost(`/tasks/${encodeURIComponent(pendingTask)}/run`, {});
        window.location.hash = '#/give/' + pendingTask;
        pendingTask = null; $('sourceResume').hidden = true;
      } catch (error) {notice(error.message, true);}
    };
    $('sourceRefresh').onclick = () => refresh().catch(() => {});
    $('sourceMore').onclick = () => refresh(true).catch(() => {});
    $('sourceMock').onclick = async () => {
      $('sourceMock').disabled = true;
      try {await generate();} catch (error) {notice(error.message, true);} finally {$('sourceMock').disabled = false;}
    };
    document.querySelectorAll('input[name="sourceChoice"]').forEach(input => input.onchange = () => {chosen.clear(); paint();});
    $('sourceUpload').onclick = async () => {
      if (mode() !== 'sources') {notice(L('Choose source files first.', 'Hãy chọn nguồn dữ liệu trước.'), true); return;}
      if (!$('sourceUploadFiles').files.length) {notice(L('Choose files to upload.', 'Chọn file để upload.'), true); return;}
      $('sourceUpload').disabled = true;
      try {
        for (const file of $('sourceUploadFiles').files) {
          notice(L('Uploading and extracting: ', 'Đang upload và đọc: ') + file.name);
          const form = new FormData(); form.append('file', file); form.append('classification', $('sourceClassification').value);
          const saved = await (await binary('/source-files', {method: 'POST', body: form})).json();
          if (saved.state === 'ready') chosen.add(saved.id);
          else notice(`${saved.filename}: ${saved.state} ${saved.error || ''}`, true);
        }
        await refresh();
      } catch (error) {notice(error.message, true);} finally {$('sourceUpload').disabled = false;}
    };
    $('newTaskForm').addEventListener('toggle', () => {if ($('newTaskForm').open) refresh().catch(() => {});});
  }
  return {init, create, task, refresh, isSourceWorkflow, assertOrdinarySources};
})();
