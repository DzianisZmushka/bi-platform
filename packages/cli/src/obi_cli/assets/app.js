// Page switching, tabs and chart drawing for a rendered dashboard. No data logic lives here:
// every value, colour and link was computed by the renderer.
(function () {
  const charts = JSON.parse(document.getElementById("charts").textContent);
  const pages = Array.from(document.querySelectorAll("section.page"));
  const drawn = new Set();

  function draw(root) {
    root.querySelectorAll(".vl").forEach(function (el) {
      if (drawn.has(el.id) || el.offsetParent === null) return;
      drawn.add(el.id);
      if (!window.vegaEmbed) {
        el.textContent = "Charts load Vega from cdn.jsdelivr.net; it could not be reached.";
        return;
      }
      vegaEmbed(el, charts[el.id], { actions: false, renderer: "svg" }).then(function (res) {
        res.view.addEventListener("click", function (_e, item) {
          const href = item && item.datum && item.datum._href;
          if (href) location.href = href;
        });
      }).catch(function (err) { el.textContent = "Chart error: " + err.message; });
    });
  }

  function show(id) {
    const target = pages.find(function (p) { return p.id === id; }) || pages[0];
    if (!target) return;
    pages.forEach(function (p) { p.hidden = p !== target; });
    document.querySelectorAll("nav.pages a").forEach(function (a) {
      a.classList.toggle("active", a.dataset.page === target.id);
    });
    document.querySelectorAll("[data-pages]").forEach(function (el) {
      const only = el.dataset.pages.split(" ").filter(Boolean);
      el.hidden = only.length > 0 && only.indexOf(target.id) < 0;
    });
    draw(target);
  }

  document.querySelectorAll(".tabs").forEach(function (tabs) {
    const buttons = Array.from(tabs.querySelectorAll(":scope > .tab-bar > button"));
    const panels = Array.from(tabs.querySelectorAll(":scope > .tab-panel"));
    function select(name) {
      buttons.forEach(function (b) { b.classList.toggle("active", b.dataset.tab === name); });
      panels.forEach(function (p) { p.hidden = p.dataset.tab !== name; });
      draw(tabs);
    }
    buttons.forEach(function (b) { b.addEventListener("click", function () { select(b.dataset.tab); }); });
    if (buttons[0]) select(buttons[0].dataset.tab);
  });
  document.querySelectorAll("details.section").forEach(function (d) {
    d.addEventListener("toggle", function () { draw(d); });
  });
  document.querySelectorAll("tr.link").forEach(function (tr) {
    tr.addEventListener("click", function () { location.href = tr.dataset.href; });
  });
  window.addEventListener("hashchange", function () { show(location.hash.slice(1)); });
  show(location.hash.slice(1));
})();
