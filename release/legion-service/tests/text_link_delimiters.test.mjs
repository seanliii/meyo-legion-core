import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../web/index_v2.html", import.meta.url), "utf8");

// Execute the shipped renderers, not a reimplementation of their URL regex.
// No page startup, browser, network, or original order state is involved.
function pageFunction(name) {
  const start = source.search(new RegExp(`^function ${name}\\(`, "m"));
  assert.ok(start >= 0, `shipped function ${name} must exist`);
  for (let end = source.indexOf("}", start); end >= 0; end = source.indexOf("}", end + 1)) {
    const code = source.slice(start, end + 1);
    try { new vm.Script(`(${code})`); return code; } catch {}
  }
  throw new Error(`Incomplete page function: ${name}`);
}

const scope = vm.createContext({});
vm.runInContext(["esc", "rich", "_verifyLinkedText"].map(pageFunction).join("\n"), scope);

function links(markup) {
  return [...markup.matchAll(/<a\b[^>]*\bhref="([^"]*)"[^>]*>([\s\S]*?)<\/a>/g)]
    .map((match) => ({ href: match[1], text: match[2] }));
}

for (const renderer of ["rich", "_verifyLinkedText"]) {
  for (const port of [8010, 8011]) {
    test(`${renderer}: inline-code delivery URL on port ${port} excludes closing backtick`, () => {
      const url = `http://127.0.0.1:${port}/index.html`;
      const output = scope[renderer](`本机入口：\`${url}\``);
      assert.deepEqual(links(output), [{ href: url, text: url }]);
      assert.equal(new URL(links(output)[0].href).pathname, "/index.html");
      assert.doesNotMatch(output, /index\.html(?:`|%60)<\/a>/);
    });
  }

  test(`${renderer}: repeated code delimiters remain outside every link`, () => {
    const one = "http://127.0.0.1:8011/index.html";
    const two = "https://example.invalid/next";
    for (const ticks of ["`", "``", "```"]) {
      const output = scope[renderer](`${ticks}${one}${ticks} 与 ${ticks}${two}${ticks}`);
      assert.deepEqual(links(output), [{ href: one, text: one }, { href: two, text: two }]);
    }
  });

  test(`${renderer}: percent-encoded delimiter bytes and URL punctuation stay intact`, () => {
    const url = "https://example.invalid/a%60b%2Ac%5Bd%5D?name=snake_case&v=%22safe%22#part_1";
    const escaped = url.replaceAll("&", "&amp;");
    const output = scope[renderer](`\`${url}\``);
    assert.deepEqual(links(output), [{ href: escaped, text: escaped }]);
    assert.match(output, /a%60b%2Ac%5Bd%5D/);
  });

  test(`${renderer}: ordinary HTTP/HTTPS links and escaped text remain supported`, () => {
    const plain = scope[renderer]("http://127.0.0.1:8011/index.html https://example.invalid/path");
    assert.deepEqual(links(plain).map((row) => row.href), [
      "http://127.0.0.1:8011/index.html", "https://example.invalid/path",
    ]);
    const escaped = scope[renderer]('<script>alert("x")</script> & `https://example.invalid/a`');
    assert.match(escaped, /&lt;script&gt;alert\(&quot;x&quot;\)&lt;\/script&gt; &amp;/);
    assert.doesNotMatch(escaped, /<script\b/i);
    assert.equal(links(escaped)[0].href, "https://example.invalid/a");
  });

  test(`${renderer}: markup-looking URL content cannot break the href attribute`, () => {
    const output = scope[renderer]('`https://example.invalid/a" onclick="alert(1)` <img onerror="evil">');
    assert.doesNotMatch(output, /<a\b[^>]*\sonclick=/i);
    assert.doesNotMatch(output, /<img\b/i);
    assert.match(output, /&lt;img onerror=&quot;evil&quot;&gt;/);
  });

  test(`${renderer}: non-HTTP schemes are still not auto-linked`, () => {
    assert.deepEqual(links(scope[renderer](
      "`javascript:alert(1)` `data:text/html,unsafe` `file:///tmp/example.html`",
    )), []);
  });
}

test("rich: existing Markdown links and media rendering are unchanged", () => {
  assert.deepEqual(links(scope.rich("[下载](https://example.invalid/a%60b.pdf)")), [{
    href: "https://example.invalid/a%60b.pdf", text: "下载",
  }]);
  assert.match(scope.rich("![图片](/media/example.png)"),
    /<img src="\/media\/example\.png" loading="lazy">/);
  assert.equal(scope.rich("第一行\n第二行"), "第一行<br>第二行");
});

for (const root of ["/home-outputs", "/outputs", "/auto-outputs", "/desktop-outputs", "/media"]) {
  test(`rich: Markdown website under ${root} is an opening link, not a forced download`, () => {
    const url = `${root}/__ut_delivery/index.html`;
    const output = scope.rich(`[打开网站](${url})`);
    assert.deepEqual(links(output), [{ href: url, text: "打开网站" }]);
    assert.match(output, /target="_blank" rel="noopener"/);
    assert.doesNotMatch(output, /<a\b[^>]*\sdownload(?:[=>\s])/i);
  });

  test(`rich: bare website under ${root} preserves query/hash but excludes backticks`, () => {
    const url = `${root}/__ut_delivery/index.html?sid=__ut_sid&v=a%60b#start`;
    const escaped = `${root}/__ut_delivery/index.html?sid=__ut_sid&amp;v=a%60b#start`;
    const output = scope.rich(`快照：\`${url}\``);
    assert.deepEqual(links(output), [{ href: escaped, text: escaped }]);
    assert.doesNotMatch(output, /<a\b[^>]*\sdownload(?:[=>\s])/i);
  });
}

test("rich: ordinary Markdown HTTP websites do not acquire a download attribute", () => {
  for (const url of ["http://127.0.0.1:8011/", "https://example.invalid/app", "https://example.invalid/index.html"]) {
    const output = scope.rich(`[打开网站](${url})`);
    assert.deepEqual(links(output), [{ href: url, text: "打开网站" }]);
    assert.doesNotMatch(output, /<a\b[^>]*\sdownload(?:[=>\s])/i);
  }
});

test("rich: explicit Markdown file downloads remain downloads", () => {
  for (const [label, url] of [
    ["下载报告", "/media/report.pdf"],
    ["下载源码", "/home-outputs/__ut_delivery/project.zip"],
    ["Download file", "https://example.invalid/report.pdf"],
  ]) {
    const output = scope.rich(`[${label}](${url})`);
    assert.deepEqual(links(output), [{ href: url, text: label }]);
    assert.match(output, /<a\b[^>]*\sdownload(?:[=>\s])/i);
  }
});

test("rich: explicit download query survives in both Markdown and bare artifact links", () => {
  const url = "/outputs/__ut_delivery/index.html?sid=__ut_sid&download=1#source";
  const escaped = "/outputs/__ut_delivery/index.html?sid=__ut_sid&amp;download=1#source";
  for (const [input, text] of [[`[源文件](${url})`, "源文件"], [`\`${url}\``, escaped]]) {
    const output = scope.rich(input);
    assert.deepEqual(links(output), [{ href: escaped, text }]);
    assert.match(output, /<a\b[^>]*\sdownload(?:[=>\s])/i);
  }
  for (const suffix of ["?download=0", "?note=download=1", "#download=1"]) {
    assert.doesNotMatch(scope.rich(`[打开网站](/outputs/__ut_delivery/index.html${suffix})`),
      /<a\b[^>]*\sdownload(?:[=>\s])/i);
  }
});

test("rich: URLs inside a Markdown label are not linkified again", () => {
  const output = scope.rich("[参考 https://example.invalid/docs 和 /media/cover.png](https://example.invalid/app)");
  assert.deepEqual(links(output), [{
    href: "https://example.invalid/app",
    text: "参考 https://example.invalid/docs 和 /media/cover.png",
  }]);
  assert.equal((output.match(/<a\b/g) || []).length, 1);
  assert.doesNotMatch(output, /<img\b/);
});

test("rich: media-like path inside an HTTP URL cannot corrupt href or src", () => {
  const url = "https://cdn.example.invalid/media/cover.png";
  for (const [input, text] of [[url, url], [`[查看](${url})`, "查看"]]) {
    const output = scope.rich(input);
    assert.deepEqual(links(output), [{ href: url, text }]);
    assert.doesNotMatch(output, /<img\b/);
  }
  assert.equal(scope.rich(`![图片](${url})`), `<img src="${url}" loading="lazy">`);
});

test("rich: existing linked Markdown images retain one clickable image", () => {
  const output = scope.rich("[![截图](/media/shot.png)](/home-outputs/__ut_delivery/index.html)");
  assert.deepEqual(links(output), [{
    href: "/home-outputs/__ut_delivery/index.html",
    text: '<img src="/media/shot.png" loading="lazy">',
  }]);
  assert.equal((output.match(/<a\b/g) || []).length, 1);
});

test("rich: bare media sentence delimiters stay outside the image or player", () => {
  for (const [input, expected] of [
    ["/media/shot.png，", '<img src="/media/shot.png" loading="lazy">，'],
    ["/media/clip.mp4。", '<video src="/media/clip.mp4" controls preload="metadata"></video>。'],
    ["/media/voice.mp3)", '<audio src="/media/voice.mp3" controls preload="none"></audio>)'],
  ]) {
    assert.equal(scope.rich(input), expected);
  }
  const download = scope.rich("/media/shot.png?download=1");
  assert.deepEqual(links(download), [{ href: "/media/shot.png?download=1", text: "/media/shot.png?download=1" }]);
  assert.match(download, /<a\b[^>]*\sdownload(?:[=>\s])/i);
  assert.doesNotMatch(download, /<img\b/);
});

for (const tail of ["。", "，", ".", ")", "）", "，）", ";", "!?"]) {
  test(`rich: bare artifact URL excludes sentence ending ${JSON.stringify(tail)}`, () => {
    for (const root of ["/home-outputs", "/outputs", "/auto-outputs", "/desktop-outputs", "/media"]) {
      const url = `${root}/__ut_delivery/index.html`;
      const output = scope.rich(`网页入口：${url}${tail}`);
      assert.deepEqual(links(output), [{ href: url, text: url }]);
      assert.ok(output.endsWith(`</a>${tail}`));
      assert.doesNotMatch(output, /<a\b[^>]*\sdownload(?:[=>\s])/i);
    }
  });
}

test("rich: explicit Markdown destinations and encoded punctuation are not trimmed", () => {
  for (const url of [
    "/outputs/name.html。", "/outputs/name.html.",
    "/outputs/name!.html?q=%2E%29#part,",
    "/outputs/name%29.html?raw=%2E%2C%29%EF%BC%89",
  ]) {
    assert.deepEqual(links(scope.rich(`[明确文件](${url})。`)), [{ href: url, text: "明确文件" }]);
    const download = scope.rich(`[下载文件](${url})。`);
    assert.deepEqual(links(download), [{ href: url, text: "下载文件" }]);
    assert.match(download, /<a\b[^>]*\sdownload(?:[=>\s])/i);
  }
});

test("rich: normal filenames, balanced brackets and escaped URL characters survive", () => {
  for (const url of [
    "/outputs/release-v1.2_(final).html", "/outputs/中文_版本-2.html",
    "/outputs/report_[final].html", "/outputs/part,one;two!.html",
    "/outputs/%E7%AB%99%E7%82%B9%20%28v2%29.html",
    "/outputs/a%60b%2Ec%29.html?raw=%2E%2C%29%EF%BC%89#part_1",
    "/outputs/report.html?label=(final)", "/outputs/report.html?label=（终稿）",
    "/outputs/report.html?value=1&",
  ]) {
    const escaped = url.replaceAll("&", "&amp;");
    assert.deepEqual(links(scope.rich(`入口 ${url}`)), [{ href: escaped, text: escaped }]);
    assert.deepEqual(links(scope.rich(`入口 ${url}。`)), [{ href: escaped, text: escaped }]);
  }
});

test("rich: Markdown, bare delivery URLs and inline media coexist without nested markup", () => {
  const output = scope.rich(
    "[网站](/home-outputs/__ut_delivery/index.html) http://127.0.0.1:8011/index.html " +
    "![截图](/media/shot.png) /media/clip.mp4 /media/voice.mp3 [下载图片](/media/cover.png)",
  );
  assert.deepEqual(links(output), [
    { href: "/home-outputs/__ut_delivery/index.html", text: "网站" },
    { href: "http://127.0.0.1:8011/index.html", text: "http://127.0.0.1:8011/index.html" },
    { href: "/media/cover.png", text: "下载图片" },
  ]);
  assert.equal((output.match(/<a\b/g) || []).length, 3);
  assert.equal((output.match(/<img\b/g) || []).length, 1);
  assert.match(output, /<video src="\/media\/clip\.mp4" controls preload="metadata"><\/video>/);
  assert.match(output, /<audio src="\/media\/voice\.mp3" controls preload="none"><\/audio>/);
  assert.equal(scope.rich("sandbox:/media/cover.png"), '<img src="/media/cover.png" loading="lazy">');
});

test("rich: controlled artifact links keep hostile labels and URL attributes escaped", () => {
  const output = scope.rich(
    '[<img src=x onerror=evil>](/home-outputs/__ut_delivery/index.html?name="onclick="evil) <script>evil</script>',
  );
  assert.deepEqual(links(output), [{
    href: "/home-outputs/__ut_delivery/index.html?name=&quot;onclick=&quot;evil",
    text: "&lt;img src=x onerror=evil&gt;",
  }]);
  assert.doesNotMatch(output, /<(?:script|img)\b|<a\b[^>]*\s(?:onclick|onerror)=/i);
  assert.match(output, /&lt;script&gt;evil&lt;\/script&gt;/);
  const comment = scope.rich("<!--rich:0--> [<!--rich:1-->](/outputs/__ut_delivery/index.html)");
  assert.match(comment, /^&lt;!--rich:0--&gt; /);
  assert.deepEqual(links(comment), [{
    href: "/outputs/__ut_delivery/index.html", text: "&lt;!--rich:1--&gt;",
  }]);
});

test("rich: unsupported routes and schemes are not partially promoted to artifact links", () => {
  for (const url of [
    "/api/legion/status", "/home-outputs-evil/index.html", "/other/home-outputs/index.html",
    "//home-outputs/index.html", "javascript:/home-outputs/index.html",
    "file:///media/report.pdf", "data:text/html,unsafe", "javascript:alert(1)",
  ]) {
    assert.deepEqual(links(scope.rich(`[不支持](${url})`)), [], url);
    assert.deepEqual(links(scope.rich(`\`${url}\``)), [], url);
  }
});

test("history linked text: existing trailing sentence punctuation stays outside links", () => {
  const url = "http://127.0.0.1:8011/index.html";
  assert.deepEqual(links(scope._verifyLinkedText(`入口 ${url}，`)), [{ href: url, text: url }]);
  assert.match(scope._verifyLinkedText(`入口 ${url}，`), /<\/a>，$/);
});
