/* Execute the actual create-task handlers offline; never calls a live API/model. */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const root = new URL('../src/ai_orchestrator/web/', import.meta.url);
const workScript = await readFile(new URL('work.js', root), 'utf8');
const scripts = await Promise.all(['source-intake.js', 'stream.js'].map(name => readFile(new URL(name, root), 'utf8')));
function harness() {
  const nodes = new Map();
  const element = id => {
    if (id === 'taskClarification' && !nodes.has(id)) return null;
    if (!nodes.has(id)) nodes.set(id, {id, value: '', files: [], checked: false, hidden: false,
      disabled: false, innerHTML: '', textContent: '', dataset: {}, options: [],
      addEventListener() {}, querySelectorAll() {return [];},
      appendChild(panel) {
        nodes.set(panel.id, panel);
        for (const match of panel.innerHTML.matchAll(/id="([^"]+)"/g)) element(match[1]);
      }});
    return nodes.get(id);
  };
  const radios = [element('sourceChooseFiles'), element('sourceChooseMock')];
  radios[0].value = 'sources'; radios[1].value = 'mock';
  const calls = [], notices = [];
  const context = vm.createContext({
    window: {}, document: {querySelector() {return radios.find(radio => radio.checked);},
      querySelectorAll() {return radios;}, addEventListener() {}, createElement() {return {innerHTML: '', className: '', id: ''};}},
    $: element, UI: {L: (en) => en, toast: text => notices.push(text)},
    state: {lang: 'en', scenarios: []},
    tr: (_key, fallback) => fallback, t_fmt: (_key, fallback, values) => fallback + JSON.stringify(values),
    esc: value => String(value), num: value => String(value),
    go() {}, route: async () => {},
    apiGet: async path => ({items: [], total: 0}),
    apiPost: async (path, body) => {calls.push({path, body: JSON.parse(JSON.stringify(body))});
      return path.endsWith('/run') ? {started: true} : {id: 'tsk_dispatch', status: 'assigned'};},
  });
  scripts.forEach((source, index) => vm.runInContext(source, context, {filename: ['source-intake.js', 'stream.js'][index]}));
  context.window.SourceUI.init();
  element('goal').value = 'Compare the schedules and identify unresolved dependencies.';
  element('taskType').value = 'analysis'; element('agent').value = 'agent_authorized';
  return {context, element, radios, calls, notices, nodes, run: () => element('runBtn').onclick()};
}

const ordinary = harness();
await ordinary.run();
assert.deepEqual(ordinary.calls.map(call => call.path), ['/tasks', '/tasks/tsk_dispatch/run']);
assert.deepEqual(ordinary.calls[0].body, {
  title: ordinary.element('goal').value.slice(0, 60), goal: ordinary.element('goal').value,
  task_type: 'analysis', start_workflow: false, owner_agent_id: 'agent_authorized',
});

const scenario = harness();
scenario.context.state.scenarios = [{key: 'schedule', title: 'Schedule assessment', agent_name: 'Design Agent',
  office: 'Middle Office', expected_output_schema: {type: 'object', required: ['risks']}}];
scenario.element('scenario').value = 'schedule';
await scenario.run();
assert.equal(scenario.calls[0].path, '/tasks');
assert.deepEqual(scenario.calls[0].body.input, {owning_department: 'Design Agent', owning_office: 'Middle Office'});
assert.deepEqual(scenario.calls[0].body.expected_output_schema, {type: 'object', required: ['risks']});

for (const workflow of ['document_review', 'quote_comparison']) {
  const source = harness(); source.element('sourceWorkflow').value = workflow;
  source.radios[0].checked = true;
  await source.run();
  assert.equal(source.calls[0].path, '/source-workflows');
  assert.equal(source.calls[0].body.workflow, workflow);
  assert.deepEqual(source.calls[0].body.selection, {mode: 'sources', file_ids: []});
}

const selected = harness(); selected.radios[0].checked = true;
await selected.run();
assert.equal(selected.calls.length, 0);
assert.ok(selected.notices.some(text => text.includes('readable source')));
selected.element('sourceClearSelection').onclick();
await selected.run();
assert.equal(selected.calls[0].path, '/tasks');

const pendingUpload = harness(); pendingUpload.element('sourceUploadFiles').files = [{name: 'authorized.txt'}];
await pendingUpload.run();
assert.equal(pendingUpload.calls.length, 0);
assert.ok(pendingUpload.notices.some(text => text.includes('Upload and select')));

const attached = harness();
attached.radios[0].checked = true;
attached.element('sourceUploadFiles').files = [new Blob(['Authorized HR policy source.'])];
attached.context.API = '/api/v1';
attached.context.authHeaders = headers => headers;
attached.context.FormData = FormData;
attached.context.fetch = async () => ({ok: true, json: async () => ({id: 'src_hr', state: 'ready'})});
await attached.element('sourceUpload').onclick();
// Native file inputs clear FileList when value is cleared; mirror that DOM behavior.
attached.element('sourceUploadFiles').files = [];
await attached.run();
assert.deepEqual(attached.calls.map(call => call.path), ['/source-workflows', '/tasks/tsk_dispatch/run']);
assert.equal(attached.calls[0].body.workflow, 'document_review');
assert.equal(attached.calls[0].body.goal, attached.element('goal').value);
assert.equal(attached.calls[0].body.owner_agent_id, 'agent_authorized');
assert.deepEqual(attached.calls[0].body.selection, {mode: 'sources', file_ids: ['src_hr']});
attached.calls.length = 0;
attached.element('sourceUploadFiles').files = [new Blob(['Unuploaded additional HR source.'])];
await attached.run();
assert.equal(attached.calls.length, 0, 'new pending files cannot be silently dropped beside old selection');
assert.ok(attached.notices.some(text => text.includes('Upload and select')));

const protectedScenario = harness();
protectedScenario.context.state.scenarios = [{key: 'contract', expected_output_schema: {type: 'object'}}];
protectedScenario.element('scenario').value = 'contract';
protectedScenario.radios[0].checked = true;
await protectedScenario.run();
assert.equal(protectedScenario.calls.length, 0);
assert.ok(protectedScenario.notices.some(text => text.includes('preserve this task contract')));

const missingSelection = harness(); missingSelection.element('sourceWorkflow').value = 'document_review';
await missingSelection.run();
assert.equal(missingSelection.calls.length, 0);
assert.ok(missingSelection.notices.some(text => text.includes('Choose source files')));
const roster = harness();
roster.context.apiGet = async () => ({
  chief: {id:'chief_active',name:'Executive Agent',lifecycle_status:'active'},
  second_tier: [{id:'office_paused',name:'Paused Office',lifecycle_status:'paused'}],
  offices: [{id:'hr_active',name:'HR Agent',lifecycle_status:'active'},
    {id:'it_paused',name:'IT Agent',lifecycle_status:'paused'},
    {id:'retired_agent',name:'Former Agent',lifecycle_status:'retired'}],
});
await roster.context.loadOwners();
assert.ok(roster.element('agent').innerHTML.includes('hr_active'));
assert.ok(roster.element('agent').innerHTML.includes('chief_active'));
assert.ok(!roster.element('agent').innerHTML.includes('it_paused'));
assert.ok(!roster.element('agent').innerHTML.includes('office_paused'));
assert.ok(!roster.element('agent').innerHTML.includes('retired_agent'));

// Load the real task-detail module and exercise clarification saving separately.
const clarification = harness();
vm.runInContext(workScript, clarification.context, {filename: 'work.js'});
clarification.context.API = '/api/v1';
clarification.context.authHeaders = headers => ({...headers, 'x-organization-id': 'org_current'});
clarification.context.parseHash = () => ({arg: 'tsk_waiting'});
clarification.context.toast = text => clarification.notices.push(text);
clarification.context.apiGet = async path => path === '/console/context' ? {actor_kind: 'human'} : {input: {}};
clarification.context.fetch = async (url, request) => {
  clarification.calls.push({url, request});
  return {ok: true, json: async () => ({id: 'tsk_waiting', status: 'assigned'})};
};
clarification.context.refreshes = [];
vm.runInContext('taskReport = {task:{id:"tsk_waiting",status:"waiting_for_input"}}; renderOneTask = async id => refreshes.push(id);', clarification.context);
await clarification.context.renderTaskClarification({id: 'tsk_waiting', status: 'waiting_for_input'});
clarification.element('taskClarificationText').value = 'Use the signed offer dated October 16.';
clarification.element('taskClarificationText').oninput();
await clarification.element('taskClarificationSave').onclick();
assert.equal(clarification.calls.length, 1);
assert.equal(clarification.calls[0].url, '/api/v1/tasks/tsk_waiting/inputs');
assert.deepEqual(JSON.parse(clarification.calls[0].request.body), {clarification: 'Use the signed offer dated October 16.'});
assert.ok(clarification.calls[0].request.headers['Idempotency-Key']);
assert.equal(clarification.calls[0].request.headers['x-organization-id'], 'org_current');
assert.equal(clarification.context.refreshes[0], 'tsk_waiting');
assert.ok(!clarification.calls.some(call => call.url?.endsWith('/run')));

const retryInput = harness();
vm.runInContext(workScript, retryInput.context);
retryInput.context.API = '/api/v1';
retryInput.context.authHeaders = headers => headers;
retryInput.context.parseHash = () => ({arg: 'tsk_retry'});
retryInput.context.toast = () => {};
retryInput.context.apiGet = async path => path === '/console/context' ? {actor_kind: 'human'} : {input:{}};
retryInput.context.fetch = async (url, request) => {
  retryInput.calls.push({url, request});
  if (retryInput.calls.length === 1) throw new Error('Connection interrupted after submit');
  return {ok:true, json:async()=>({id:'tsk_retry',status:'assigned'})};
};
vm.runInContext('taskReport={task:{id:"tsk_retry",status:"waiting_for_input"}};renderOneTask=async()=>{};', retryInput.context);
await retryInput.context.renderTaskClarification({id:'tsk_retry',status:'waiting_for_input'});
await retryInput.element('taskClarificationSave').onclick();
assert.equal(retryInput.calls.length, 0, 'empty clarification must not be submitted');
retryInput.element('taskClarificationText').value = 'Use the latest signed policy.';
retryInput.element('taskClarificationText').oninput();
await retryInput.element('taskClarificationSave').onclick();
await retryInput.element('taskClarificationSave').onclick();
assert.equal(retryInput.calls.length, 2);
assert.equal(retryInput.calls[0].request.headers['Idempotency-Key'], retryInput.calls[1].request.headers['Idempotency-Key']);

const specialized = harness();
vm.runInContext(workScript, specialized.context);
specialized.context.parseHash = () => ({arg: 'tsk_source'});
specialized.context.apiGet = async path => path === '/console/context' ? {actor_kind: 'human'} : {input:{source_workflow:'document_review'}};
vm.runInContext('taskReport={task:{id:"tsk_source",status:"waiting_for_input"}}', specialized.context);
await specialized.context.renderTaskClarification({id:'tsk_source',status:'waiting_for_input'});
assert.equal(specialized.nodes.has('taskClarification'), false);
console.log(JSON.stringify({passed: true, checks: ['ordinary payload', 'scenario contract', 'two explicit source workflows',
  'ordinary selected uploads use cited-report runtime preserving HR goal/owner', 'scenario contract never silently replaced', 'explicit clear allows ordinary', 'pending upload not silently dropped',
  'source workflow requires selection mode', 'clarification saves with tenant/idempotency and never auto-runs',
  'specialized source workflow keeps its own input controller', 'empty clarification rejected',
  'ambiguous save retries same idempotency key', 'owner picker excludes paused/retired agents and keeps active HR'], live_requests: 0}));
