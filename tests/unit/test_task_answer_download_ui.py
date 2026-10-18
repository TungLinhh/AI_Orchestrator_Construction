"""Execute the actual answer renderer and download handler in Node."""

import subprocess
from pathlib import Path


def test_answer_download_button_is_completed_only_and_uses_authorized_endpoint():
    source = Path("src/ai_orchestrator/web/work.js").read_text()
    renderer = source.split("function renderAnswer(t) {", 1)[1].split("/** Full answer texts", 1)[0]
    script = r"""
const assert = require('node:assert/strict');
const elements = {};
for (const id of ['taskAnswer', 'taskAnswerSub', 'taskAnswerCopy', 'taskAnswerEmpty']) {
  elements[id] = {hidden: false, after(button) {elements[button.id] = button;}};
}
const $ = id => elements[id];
const document = {createElement(tag) {return {click() {this.clicked = true; links.push(this);}};}};
const links = [], calls = [], errors = [];
const UI = {L: en => en}, API = '/api/v1';
const authHeaders = () => ({Authorization: 'Bearer test-only'});
const setHTML = () => {}, tr = (key, fallback) => fallback;
const t_fmt = () => '', plain = value => String(value), humanKey = value => value;
const esc = value => value, toast = (...args) => errors.push(args);
let answerTexts;
const fetch = async (url, options) => {
  calls.push({url, options});
  return {ok: true, blob: async () => new Blob(['complete answer'])};
};
"""
    script += "\nfunction renderAnswer(t) {" + renderer
    script += r"""
(async () => {
  renderAnswer({id: 'tsk_test', status: 'running', output: {summary: 'partial'}});
  assert.equal(elements.taskAnswerDownload.hidden, true);
  assert.equal(calls.length, 0);
  renderAnswer({id: 'tsk_test', status: 'completed', output: {summary: 'Full answer'}});
  assert.equal(elements.taskAnswerDownload.hidden, false);
  assert.equal(elements.taskAnswerDownload.textContent, 'Download answer');
  await elements.taskAnswerDownload.onclick();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/v1/tasks/tsk_test/answer/download');
  assert.equal(calls[0].options.headers.Authorization, 'Bearer test-only');
  assert.equal(links[0].download, 'task-answer.md');
  assert.equal(links[0].clicked, true);
  renderAnswer({id: 'tsk_empty', status: 'completed', output: {}});
  assert.equal(elements.taskAnswerDownload.hidden, true);
  assert.deepEqual(errors, []);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
