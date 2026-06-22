/*
 * In-browser mode: parse and render the AFP file entirely in the browser via
 * Pyodide (our Python compiled to WebAssembly), so the file is NEVER uploaded.
 * This is the privacy / DLP-safe path — nothing leaves the user's computer.
 *
 * It intercepts the upload form's submit, runs the same readafp parser +
 * template client-side, and replaces the page with the rendered result. If
 * the engine can't load (e.g. a network blocks the CDN), it falls back to a
 * normal server submit so the tool still works for unrestricted users.
 */
(function () {
  "use strict";
  var PY = "https://cdn.jsdelivr.net/pyodide/v0.27.2/full/";
  var pyodideReady = null;

  function overlay(msg) {
    var el = document.getElementById("inbrowser-overlay");
    if (!el) {
      var st = document.createElement("style");
      st.textContent = "@keyframes ibspin{to{transform:rotate(360deg)}}";
      document.head.appendChild(st);
      el = document.createElement("div");
      el.id = "inbrowser-overlay";
      el.style.cssText =
        "position:fixed;inset:0;z-index:9999;display:flex;align-items:center;" +
        "justify-content:center;background:rgba(8,10,16,.88);color:#e4e6f0;" +
        "font:600 1rem system-ui,'Segoe UI',Arial;backdrop-filter:blur(2px)";
      el.innerHTML =
        '<div style="text-align:center;max-width:440px;padding:24px">' +
        '<div style="width:34px;height:34px;margin:0 auto 16px;border:4px solid ' +
        '#313650;border-top-color:#26c6da;border-radius:50%;animation:ibspin 1s ' +
        'linear infinite"></div><div id="inbrowser-msg"></div>' +
        '<div style="margin-top:10px;color:#9aa0b8;font-weight:400;font-size:.85rem">' +
        "Your file is processed here in your browser — it never leaves your " +
        "computer.</div></div>";
      document.body.appendChild(el);
    }
    el.style.display = "flex";
    document.getElementById("inbrowser-msg").textContent = msg;
  }
  function hideOverlay() {
    var el = document.getElementById("inbrowser-overlay");
    if (el) el.style.display = "none";
  }

  function loadScript(src) {
    return new Promise(function (res, rej) {
      var s = document.createElement("script");
      s.src = src;
      s.onload = res;
      s.onerror = function () { rej(new Error("failed to load " + src)); };
      document.head.appendChild(s);
    });
  }

  function ensurePyodide() {
    if (pyodideReady) return pyodideReady;
    pyodideReady = (async function () {
      overlay("Loading the in-browser engine… (first time only)");
      await loadScript(PY + "pyodide.js");
      var py = await loadPyodide({ indexURL: PY });
      await py.loadPackage("micropip");
      var micropip = py.pyimport("micropip");
      await micropip.install(["segno", "jinja2"]);
      var zip = await (await fetch("/pyodide/readafp.zip")).arrayBuffer();
      py.unpackArchive(zip, "zip");
      return py;
    })();
    return pyodideReady;
  }

  var RENDER =
    "import jinja2, readafp.app as A\n" +
    "_ctx = A.build_context(bytes(FILE_BYTES.to_py()), FILE_NAME, FILE_CP, FILE_EMBED)\n" +
    "_env = jinja2.Environment(loader=jinja2.FileSystemLoader('readafp/templates'), autoescape=True)\n" +
    "_env.globals['url_for'] = lambda endpoint, **k: ('/static/' + k.get('filename','')) if endpoint=='static' else '/'\n" +
    "_env.get_template('index.html').render(**_ctx)\n";

  async function handle(form, ev) {
    var fileInput = form.querySelector('input[type=file]');
    if (!fileInput || !fileInput.files || !fileInput.files[0]) return;
    ev.preventDefault();
    var file = fileInput.files[0];
    var sel = form.querySelector('select[name=codepage]');
    var cp = sel ? sel.value : "cp500";
    var embedBox = form.querySelector('input[name=embed_small_fonts]');
    var embed = !!(embedBox && embedBox.checked);
    try {
      var py = await ensurePyodide();
      overlay("Reading your file locally…");
      var bytes = new Uint8Array(await file.arrayBuffer());
      py.globals.set("FILE_BYTES", bytes);
      py.globals.set("FILE_NAME", file.name);
      py.globals.set("FILE_CP", cp);
      py.globals.set("FILE_EMBED", embed);
      var html = await py.runPythonAsync(RENDER);
      document.open();
      document.write(html);
      document.close();
    } catch (err) {
      console.error("in-browser processing failed; falling back to server:", err);
      hideOverlay();
      form.removeEventListener("submit", form.__ib);
      form.submit();
    }
  }

  function attach() {
    var form = document.querySelector('form[action="/inspect"]');
    if (!form) return;
    form.__ib = function (ev) { handle(form, ev); };
    form.addEventListener("submit", form.__ib);
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", attach);
  else attach();
})();
