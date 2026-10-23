(function (root) {
  "use strict";

  // Same rule as scripts/shuli.py lookup(): (num - 1) % 80 + 1
  function normalize(num) {
    if (!Number.isInteger(num) || num <= 0) {
      throw new Error("输入的数字必须是正整数！");
    }
    return ((num - 1) % 80) + 1;
  }

  function parse(text) {
    var s = String(text).trim();
    if (!/^\d+$/.test(s)) throw new Error("输入的数字必须是正整数！");
    if (/^0+$/.test(s)) throw new Error("输入的数字必须是正整数！");
    // 10000 是 80 的倍数，所以只看末四位，结果与完整数字相同
    var n = Number(s.slice(-4));
    return n === 0 ? 10000 : n;
  }

  var FIELDS = [
    ["summary", "概要"],
    ["foundation", "基础运"],
    ["family", "家庭运"],
    ["health", "健康运"],
    ["meaning", "详解"]
  ];

  function render(out, entry, input) {
    out.textContent = "";
    var head = document.createElement("h3");
    head.textContent = entry.number + " · " + entry.title;
    var tag = document.createElement("span");
    tag.className = "shuli-tag shuli-" + entry.category;
    tag.textContent = entry.category;
    head.appendChild(document.createTextNode(" "));
    head.appendChild(tag);
    out.appendChild(head);
    if (input !== entry.number) {
      var note = document.createElement("p");
      note.textContent = input + " 除以 80 取余后对应 " + entry.number;
      out.appendChild(note);
    }
    FIELDS.forEach(function (f) {
      var p = document.createElement("p");
      var b = document.createElement("strong");
      b.textContent = f[1] + "：";
      p.appendChild(b);
      p.appendChild(document.createTextNode(entry[f[0]]));
      out.appendChild(p);
    });
  }

  function init(el) {
    var input = el.querySelector("input");
    var out = el.querySelector(".shuli-result");
    var data = null;

    function update() {
      if (!data) return;
      try {
        var n = parse(input.value);
        render(out, data[normalize(n)], n);
      } catch (e) {
        out.textContent = input.value.trim() === "" ? "" : e.message;
      }
    }

    el.querySelector("form").addEventListener("submit", function (e) {
      e.preventDefault();
      update();
    });

    fetch(el.getAttribute("data-src"))
      .then(function (r) { return r.json(); })
      .then(function (d) { data = d; update(); })
      .catch(function () { out.textContent = "数据加载失败"; });
  }

  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", function () {
      document.querySelectorAll(".shuli-calc").forEach(init);
    });
  }
  root.ShuliCalc = { normalize: normalize, parse: parse };
})(typeof window !== "undefined" ? window : globalThis);
