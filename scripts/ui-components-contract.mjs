// Pure component trust boundaries and local route availability; no API or database.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
const icons = await readFile("src/ai_orchestrator/web/icons.js", "utf8");
const components = await readFile(
  "src/ai_orchestrator/web/components.js",
  "utf8",
);
const guide = await readFile("src/ai_orchestrator/web/styleguide.js", "utf8");
function environment(hostname) {
  return vm.createContext({
    state: { lang: "vi" },
    esc: (value) =>
      String(value).replace(
        /[&<>"']/g,
        (c) =>
          ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;",
          })[c],
      ),
    document: { addEventListener() {} },
    window: { addEventListener() {} },
    location: { hostname, href: "http://" + hostname + "/api/v1/ui" },
    URL,
    ROUTES: { give: { label: "Work" } },
    MANAGEMENT_VIEWS: new Map(),
  });
}
for (const hostname of ["localhost", "127.0.0.1", "console.example.test"]) {
  const ctx = environment(hostname);
  vm.runInContext(icons + "\n" + components + "\n" + guide, ctx);
  assert.equal(
    vm.runInContext("UIStyleguide.local", ctx),
    hostname !== "console.example.test",
  );
  assert.equal(
    vm.runInContext("MANAGEMENT_VIEWS.has('_styleguide')", ctx),
    hostname !== "console.example.test",
  );
  const hostile = '<img src=x onerror="alert(1)">';
  ctx.hostile = hostile;
  for (const expression of [
    "UI.button({label:hostile})",
    "UI.input({label:hostile,value:hostile,error:hostile})",
    "UI.select({label:hostile,options:[{value:hostile,label:hostile}]})",
    "UI.textarea({label:hostile,value:hostile})",
    "UI.badge(hostile,hostile)",
    'UI.listRow({title:hostile,meta:hostile,error:hostile,url:"javascript:alert(1)"})',
    "UI.copyId(hostile)",
    "UI.logViewer({label:hostile,text:hostile})",
  ]) {
    const html = vm.runInContext(expression, ctx);
    assert(!html.includes("<img"), expression);
    assert(!html.includes('href="javascript:'), expression);
    assert(html.includes("&lt;img"), expression);
  }
  for (const url of [
    "javascript:alert(1)",
    "//other.example",
    "/\\other.example",
    "/\n/other.example",
  ]) {
    ctx.url = url;
    assert(
      vm
        .runInContext('UI.listRow({title:"safe",url})', ctx)
        .includes('href="#"'),
    );
  }
  ctx.url = "#/give/sample";
  assert(
    vm
      .runInContext('UI.listRow({title:"safe",url})', ctx)
      .includes('href="#/give/sample"'),
  );
  assert(!vm.runInContext("UIIcon(hostile)", ctx).includes("<img"));
  assert(
    vm
      .runInContext('UI.button({label:"wait",loading:true})', ctx)
      .includes("disabled"),
  );
}
console.log(
  "PASS: local route guard, text/attribute escaping, unsafe links, icons and loading controls",
);
