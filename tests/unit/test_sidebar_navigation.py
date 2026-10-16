"""Exercise actual router visibility and sidebar selection without network writes."""

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "src/ai_orchestrator/web"


def test_routes_show_rendered_panel_and_select_parent_navigation():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for console router checks")
    harness = r"""
const assert = require('node:assert/strict');
const elements = new Map();
function $(id) {
  if (!elements.has(id)) elements.set(id, {hidden:false, open:false, textContent:'Department',
    classList:{add(){}}, close(){this.open=false;}});
  return elements.get(id);
}
const anchors = ['#/give','#/departments','#/work/issues','#/work/approvals'].map(href=>({
  dataset:{nav:href.split('/')[1]}, href, current:null,
  getAttribute(){return href}, setAttribute(k,v){this.current=v},
  removeAttribute(){this.current=null}
}));
const location={href:'http://localhost/ui?org=tenant',hash:'#/departments'};
const history={state:null,replaceState(s){this.state=s}};
const document={addEventListener(){},querySelectorAll(){return anchors}};
const window={addEventListener(){}};
const state={lang:'en'};
const tr=(k,v)=>v, esc=String, setHTML=()=>{}, paintProvider=()=>{}, taskName=String;
let rendered;
async function renderDepartments(arg){rendered=arg?'dept':'departments'}
async function renderGive(){rendered='give'}
async function renderWork(){rendered='work'}
async function renderApproval(){rendered='work'}
"""
    checks = r"""
(async()=>{
for (const [hash,panel,nav] of [
 ['#/departments','departments','#/departments'],
 ['#/dept/chief','dept','#/departments'],
 ['#/departments/office','dept','#/departments'],
 ['#/give/task/steps/run','give','#/give'],
 ['#/task/task','give','#/give'],
 ['#/approval/id','work','#/work/approvals'],
 ['#/work/issues','work','#/work/issues']]) {
 location.hash=hash; await route();
 assert.equal($('pageError').hidden,true,hash);
 assert.equal(rendered,panel,hash);
 const visible=[...elements.entries()]
   .filter(([id,el])=>id.startsWith('view-')&&!el.hidden).map(([id])=>id);
 assert.deepEqual(visible,['view-'+panel],hash);
 assert.deepEqual(anchors.filter(a=>a.current).map(a=>a.href),[nav],hash);
}
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(
        [node, "-e", harness + (WEB / "navigation.js").read_text() + checks],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_auth_disabled_boot_is_ready_without_identity_calls():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for console authentication checks")
    harness = r"""
const assert=require('node:assert/strict');
const AUTH_OFF=true, ORG='org_01m4zffad8ejxeje1p8898d65a';
const window={};
const controls=new Map();
const document={getElementById(id){
 if(!controls.has(id)) controls.set(id,{hidden:false});
 return controls.get(id);
}};
const UI={L:(en)=>en};
function token(){throw Error('Auth-off boot must not read an identity token')}
function fetchMe(){throw Error('Auth-off boot must not call identity/me')}
"""
    checks = r"""
window.UIAuth.verifySessionOnBoot().then(ready=>{
 assert.equal(ready,true);
 for(const id of ['tokenBtn','loginBtn','memberDisplay'])
   assert.equal(controls.get(id).hidden,true,id);
}).catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(
        [node, "-e", harness + (WEB / "auth-ui.js").read_text() + checks],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "window.AUTH_OFF" not in (WEB / "boot.js").read_text()


def test_failed_signout_retains_session_for_retry():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for console authentication checks")
    harness = r"""
const assert=require('node:assert/strict');
const AUTH_OFF=false, ORG='org_01m4zffad8ejxeje1p8898d65a';
let handler, cleared=false, reloaded=false;
const button={disabled:false,setAttribute(){},removeAttribute(){},
 addEventListener(event,fn){handler=fn}};
const document={getElementById(id){return id==='signoutBtn'?button:null}};
const UI={L:(en)=>en}, confirm=()=>true, toast=()=>{};
const window={location:{reload(){reloaded=true}}};
async function logoutHuman(){throw Error('Server unavailable')}
function clearHumanSession(){cleared=true}
"""
    checks = r"""
window.UIAuth.init();
handler().then(()=>{
 assert.equal(cleared,false);
 assert.equal(reloaded,false);
 assert.equal(button.disabled,false);
}).catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(
        [node, "-e", harness + (WEB / "auth-ui.js").read_text() + checks],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
