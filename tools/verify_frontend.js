// 前端 app.js owner 自选管理代码路径验证（Node 侧）
// 目的：证明网页里「加入/删除」触发的 fetch 真的构造正确，
//       打到正确的 GitHub endpoint、带正确的 body。
// 做法：桩掉 fetch 捕获请求，加载 app.js，注入 DATA._admin，调用 _pushWatchToCloud。
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const ROOT = "C:\\Users\\Basshunter-j\\ZCodeProject\\astock-system";
const appSrc = fs.readFileSync(path.join(ROOT, "site_template", "app.js"), "utf8");

const calls = [];
const sandbox = {
  console,
  setTimeout,
  document: {
    getElementById: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    createElement: () => ({ style: {}, setAttribute: () => {}, appendChild: () => {} }),
  },
  fetch: async (url, opts) => {
    calls.push({ url, opts });
    return {
      ok: true, status: 204,
      json: async () => ({}),
    };
  },
  window: {},
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

// app.js 是脚本（非模块），顶部有 "use strict" 和 let DATA
vm.runInContext(appSrc + "\n;sandboxExport();", vm.createContext(Object.assign(sandbox, {
  sandboxExport: () => {},
})), { timeout: 10000 });

const win = sandbox;

// DATA 是 let 声明，在 vm 上下文里；重新求值注入
vm.runInContext(`
  DATA = {
    date: "2026-09-15",
    _admin: {
      repo: "aprildream24/astock-system",
      workflow: "stock.yml",
      ref: "main",
      secret: "WATCH_CODES",
      token: "ghp_TESTSENTINELTOKEN0000000000000000000000",
      enabled: true,
    },
    watch_advice: [],
  };
`, sandbox);

(async () => {
  let ok = true;
  const fail = (m) => { ok = false; console.log("  ✗ " + m); };
  const pass = (m) => console.log("  ✓ " + m);

  console.log("=".repeat(62));
  console.log("前端 owner 自选管理 · 代码路径验证");
  console.log("=".repeat(62));

  if (typeof sandbox._normCode !== "function") fail("_normCode 未定义");
  else {
    pass("_normCode 存在");
    const cases = [["600519", "sh600519"], ["000001", "sz000001"],
                   ["sh600519", "sh600519"], ["SH600519", "sh600519"],
                   ["300750", "sz300750"]];
    for (const [inp, want] of cases) {
      const got = sandbox._normCode(inp);
      if (got === want) pass(`_normCode(${inp}) = ${got}`);
      else fail(`_normCode(${inp}) = ${got}，期望 ${want}`);
    }
    if (sandbox._normCode("60051") === null) pass("_normCode 拒绝 5 位");
    else fail("_normCode 未拒绝 5 位");
  }

  if (typeof sandbox._pushWatchToCloud !== "function") { fail("_pushWatchToCloud 未定义"); }
  else {
    pass("_pushWatchToCloud 存在");
    calls.length = 0;
    await sandbox._pushWatchToCloud(["sh600519", "sz000001", "sh600088"]);
    if (calls.length !== 1) fail(`应发 1 个请求，实发 ${calls.length}`);
    else {
      const c = calls[0];
      pass(`请求数 = 1`);
      const wantUrl = "https://api.github.com/repos/aprildream24/astock-system/actions/workflows/stock.yml/dispatches";
      if (c.url === wantUrl) pass(`URL 正确：${c.url}`);
      else fail(`URL 错：${c.url}`);

      if (c.opts && c.opts.method === "POST") pass("method = POST");
      else fail(`method = ${c.opts && c.opts.method}`);

      const h = c.opts.headers || {};
      const expectAuth = "Bearer ghp_TESTSENTINELTOKEN0000000000000000000000";
      if (h["Authorization"] === expectAuth) pass("Authorization 带 owner 令牌");
      else fail("Authorization 缺失/错误：" + h["Authorization"]);

      let body;
      try { body = JSON.parse(c.opts.body); } catch (e) { fail("body 非 JSON"); }
      if (body) {
        if (body.ref === "main") pass("body.ref = main");
        else fail("body.ref = " + body.ref);
        if (body.inputs && body.inputs.task === "watch-sync") pass("body.inputs.task = watch-sync");
        else fail("task 错：" + JSON.stringify(body.inputs));
        const codes = body.inputs && body.inputs.codes;
        if (codes === "sh600519,sz000001,sh600088") pass("codes 逗号拼接正确：" + codes);
        else fail("codes 错：" + codes);
      }

      // 令牌不得出现在 URL 里（应只在 Authorization 头）
      if (!c.url.includes("ghp_") && !c.url.includes("TESTSENTINEL")) pass("URL 不含令牌");
      else fail("令牌泄漏到 URL");
    }
  }

  console.log("\n" + (ok ? "全部通过 ✓" : "有失败 ✗"));
  process.exit(ok ? 0 : 1);
})();
