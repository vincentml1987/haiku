"""
Markdown renderer tests (daemon/ui/app.js), written by Tessera as reviewer
after the 2026-10-04 review of b550f86. Run directly: `python test_markdown.py`. Set HAIKU_APP_JS to test another copy
of app.js (e.g. an old commit, as a negative control).

There is no Node here, so the renderer runs in headless Chrome: this script
extracts the markdown section of app.js (plus the el() helper) into a
throwaway HTML page, loads it with --dump-dom, and reads the results back.
Skips (exit 0, says so) when Chrome is not installed. Uses a temp directory
only. Source is ASCII with \\u escapes on purpose: no literal invisible
characters in committed test source.

Two things are checked:
  1. Time: hostile input must render in bounded time. The original renderer
     was quadratic (24 KB of "*a " took ~1 s, 1 MB would hang for minutes).
  2. Safety: a corpus of injection attempts must produce no executable or
     loading elements, no non-http(s)/mailto links, and no on* attributes.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP_JS = Path(os.environ.get("HAIKU_APP_JS") or Path(__file__).parent / "ui" / "app.js")
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

# Generous: the fix measures under 300 ms at 1 MB; this leaves room for a
# slow or busy machine while still failing on any return to quadratic cost.
LIMIT_MS = 3000
SIZES = (100_000, 1_000_000)

TIMING_CASES = {
    "star-a": "*a ",
    "tilde-a": "~~a ",
    "under-a": "_a ",
    "open-bracket": "[",
    "close-bracket": "]",
    "bang-bracket": "![",
    "link-open": "[a](",
    "link-unclosed-url": "[a](x ",
    "double-star": "**",
    "backticks": "`a",
    "mixed": "*[_`~",
    "autolink-open": "<http://a",
    "bare-urls": "http://a.b.",
    "nested-quote": "> ",
    "nested-list": "- ",
}

SAFETY_CASES = [
    "<script>window.__pwned = 1</script>",
    "<img src=x onerror=\"window.__pwned = 1\">",
    "[x](javascript:window.__pwned=1)",
    "[x](JaVaScRiPt:window.__pwned=1)",
    "[x](java\tscript:window.__pwned=1)",
    "[x](java\nscript:window.__pwned=1)",
    "[x](data:text/html,<script>window.__pwned=1</script>)",
    "[x](vbscript:msgbox(1))",
    "![x](http://example.invalid/pixel.png)",
    "![x](javascript:window.__pwned=1)",
    "<javascript:window.__pwned=1>",
    "[x](  javascript:window.__pwned=1  )",
    "[x](//evil.example/path)",
    "[x](file:///C:/Windows/win.ini)",
    "<iframe src=\"http://evil.example\"></iframe>",
    "<svg onload=\"window.__pwned=1\"></svg>",
    "<style>body{display:none}</style>",
    "<form action=http://evil.example><input name=a></form>",
    "# <b onclick=1>heading</b>",
    "> <a href=\"javascript:1\">quoted</a>",
    "- <a href=\"javascript:1\">item</a>",
    "```\n<script>window.__pwned=1</script>\n```",
    "`<script>window.__pwned=1</script>`",
    "**<img src=x onerror=1>**",
    "[a\\]b](http://ok.example)",
    "\u202e\u2028\u200b raw invisibles",
]

HARNESS_TAIL = r"""
const TIMING = %(timing)s;
const SIZES = %(sizes)s;
const SAFETY = %(safety)s;
const out = {timing: [], safety: []};
for (const [name, unit] of Object.entries(TIMING)) {
  for (const size of SIZES) {
    const text = unit.repeat(Math.ceil(size / unit.length)).slice(0, size);
    const t = performance.now();
    let err = null;
    try { renderMarkdown(text); } catch (e) { err = String(e); }
    out.timing.push({name, size, ms: Math.round(performance.now() - t), err});
  }
}
const BAD_TAGS = new Set(['SCRIPT','IMG','IFRAME','OBJECT','EMBED','STYLE','FORM','INPUT','SVG','LINK','META','BASE','VIDEO','AUDIO','SOURCE']);
for (const text of SAFETY) {
  const frag = renderMarkdown(text);
  const host = document.createElement('div');
  host.appendChild(frag);
  const problems = [];
  for (const n of host.querySelectorAll('*')) {
    if (BAD_TAGS.has(n.tagName.toUpperCase())) problems.push('element ' + n.tagName);
    for (const a of n.attributes) {
      if (/^on/i.test(a.name)) problems.push('attribute ' + a.name);
      if (a.name === 'style' || a.name === 'srcdoc') problems.push('attribute ' + a.name);
    }
    if (n.tagName === 'A') {
      const h = n.getAttribute('href') || '';
      if (!/^(https?:|mailto:)/i.test(h)) problems.push('link href ' + h.slice(0, 40));
      if ((n.getAttribute('rel') || '').indexOf('noopener') < 0) problems.push('link without noopener');
    }
  }
  out.safety.push({text: text.slice(0, 50), problems});
}
out.pwned = window.__pwned === undefined ? false : true;
document.getElementById('out').textContent = JSON.stringify(out);
"""


def find_chrome():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def build_page(tmp: str) -> str:
    src = APP_JS.read_text(encoding="utf-8").split("\n")
    s = next(i for i, l in enumerate(src) if l.startswith("function el("))
    e = next(i for i in range(s, len(src)) if src[i] == "}")
    el = "\n".join(src[s:e + 1])
    m0 = next(i for i, l in enumerate(src) if l.startswith("/* ---------- markdown"))
    m1 = next(i for i, l in enumerate(src) if l.startswith("/* ---------- API"))
    md = "\n".join(src[m0:m1])
    def js(v):
        # "</script>" inside an inline script would end the block early.
        return json.dumps(v).replace("</", "<" + chr(92) + "/")

    tail = HARNESS_TAIL % {
        "timing": js(TIMING_CASES),
        "sizes": js(list(SIZES)),
        "safety": js(SAFETY_CASES),
    }
    page = os.path.join(tmp, "md-test.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write("<!doctype html><meta charset=utf-8><pre id=out>pending</pre><script>\n"
                + el + "\n" + md + "\n" + tail + "\n</script>")
    return page


def check(label, cond):
    print(f"[{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        raise AssertionError(label)


def main():
    chrome = find_chrome()
    if chrome is None:
        print("SKIP: Chrome not found, markdown tests not run")
        return 0
    tmp = tempfile.mkdtemp(prefix="haiku-md-")
    try:
        page = build_page(tmp)
        profile = os.path.join(tmp, "profile")
        cmd = [chrome, "--headless=new", "--disable-gpu", f"--user-data-dir={profile}",
               "--virtual-time-budget=120000", "--dump-dom", Path(page).as_uri()]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        except subprocess.TimeoutExpired:
            check("the renderer finished all cases within 120 s (a hang means quadratic cost is back)", False)
        dom = proc.stdout
        marker = '<pre id="out">'
        start = dom.find(marker)
        check("harness page produced output", start >= 0)
        body = dom[start + len(marker):dom.find("</pre>", start)]
        check("harness did not hang or hit a script error ('pending' replaced)", body != "pending")
        body = body.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
        res = json.loads(body)

        for r in res["timing"]:
            label = f"{r['name']} at {r['size'] // 1000} KB renders in under {LIMIT_MS} ms (took {r['ms']} ms)"
            check(label, r["err"] is None and r["ms"] < LIMIT_MS)
        for r in res["safety"]:
            check(f"safe output for: {ascii(r['text'])}", not r["problems"])
        check("no injected script ran (window.__pwned untouched)", res["pwned"] is False)
        print("\nmarkdown tests passed")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
