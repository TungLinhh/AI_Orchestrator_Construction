/* Source choice is explicit. Uploaded and mock files share the same API/parser. */
window.SourceUI = (() => {
  const chosen = new Set();
  let library = [];
  let pendingTask = null;
  let libraryOffset = 0;
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
      $('sourceMore').hidden = libraryOffset >= page.total;
      paint(); notice(L('Select only files this task may use.', 'Chỉ chọn những file được phép dùng cho công việc này.'));
    } catch (error) { notice(error.message, true); throw error; }
  }
  async function generate() {
    if (mode() !== 'mock') throw new Error(L('Choose mock sources first.', 'Hãy chọn tạo file mock trước.'));
    const result = await apiPost('/source-files/mock?workflow=' + encodeURIComponent($('sourceWorkflow').value));
    chosen.clear(); result.items.forEach(file => chosen.add(file.id));
    await refresh(); notice(L('Actual mock files generated and extracted.', 'Đã tạo và đọc file mock qua parser.'));
  }
  async function create(body) {
    if (!mode()) throw new Error(L('Choose source files or generate mock files before running.', 'Bạn muốn chọn nguồn dữ liệu hay tạo file mock để kiểm thử?'));
    if (mode() === 'mock' && !chosen.size) await generate();
    const ids = [...chosen];
    if (mode() === 'mock' && ids.some(id => !library.find(f => f.id === id)?.synthetic)) {
      throw new Error(L('Mock mode only accepts generated mock files.', 'Chế độ mock chỉ nhận file mock được tạo trong hệ thống.'));
    }
    return apiPost('/source-workflows', {...body, workflow: $('sourceWorkflow').value, selection: {mode: mode(), file_ids: ids}});
  }
  async function task(id) {
    try {
      const [source, artifacts] = await Promise.all([apiGet(`/tasks/${encodeURIComponent(id)}/sources`), apiGet(`/tasks/${encodeURIComponent(id)}/artifacts`)]);
      if (parseHash().arg !== id) return;
      const panel = $('sourceTaskPanel');
      panel.hidden = !source.manifest && !artifacts.items.length;
      if (panel.hidden) {
        const detail = await apiGet(`/tasks/${encodeURIComponent(id)}`);
        if (parseHash().arg !== id) return;
        if (!detail.input?.source_workflow) return;
        panel.hidden = false;
        $('sourceTaskFiles').innerHTML = `<p>${esc(detail.last_error || L('This workflow needs sources. Select files in the new-task form, then attach them here.', 'Luồng đang cần nguồn. Chọn file ở form giao việc, rồi gắn vào task tại đây.'))}</p><button class="ui-btn ui-btn-secondary ui-btn-md" id="sourcePickForTask">${L('Choose sources for this task', 'Chọn nguồn cho task này')}</button><button class="ui-btn ui-btn-secondary ui-btn-md" id="sourceAttach">${L('Attach selected files', 'Gắn các file đang chọn')}</button>`;
        $('sourcePickForTask').onclick = () => {
          pendingTask = id; window.location.hash = '#/give';
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
    } catch (error) {
      if (parseHash().arg === id) { $('sourceTaskPanel').hidden = false; $('sourceTaskFiles').textContent = error.message; }
    }
  }
  function init() {
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
  return {init, create, task, refresh};
})();
