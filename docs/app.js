(function () {
  "use strict";

  // ---- language toggle -----------------------------------------------------
  var STORAGE_KEY = "crewmeal-lang";
  var buttons = document.querySelectorAll(".lang button");

  function apply(lang) {
    document.documentElement.setAttribute("lang", lang === "en" ? "en" : "ko");
    document.querySelectorAll("[data-ko]").forEach(function (el) {
      var val = el.getAttribute("data-" + lang);
      if (val !== null) el.textContent = val;
    });
    buttons.forEach(function (b) {
      b.classList.toggle("active", b.dataset.lang === lang);
    });
    try { localStorage.setItem(STORAGE_KEY, lang); } catch (e) {}
  }

  buttons.forEach(function (b) {
    b.addEventListener("click", function () { apply(b.dataset.lang); });
  });

  var saved = "ko";
  try { saved = localStorage.getItem(STORAGE_KEY) || "ko"; } catch (e) {}
  apply(saved);

  // ---- lightbox ------------------------------------------------------------
  var box = document.getElementById("lightbox");
  var boxImg = box ? box.querySelector("img") : null;

  document.querySelectorAll(".zoom img").forEach(function (img) {
    img.addEventListener("click", function () {
      if (!box || !boxImg) return;
      boxImg.src = img.src;
      boxImg.alt = img.alt;
      box.classList.add("open");
    });
  });
  if (box) {
    box.addEventListener("click", function () { box.classList.remove("open"); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") box.classList.remove("open");
    });
  }

  // ---- scroll progress + scrollspy ----------------------------------------
  var progress = document.getElementById("progress");
  var links = Array.prototype.slice.call(
    document.querySelectorAll('.nav-links a[href^="#"]')
  );
  var sections = links.map(function (a) {
    return document.getElementById(a.getAttribute("href").slice(1));
  });
  var ticking = false;

  function update() {
    ticking = false;
    var doc = document.documentElement;
    var scrollY = window.scrollY || window.pageYOffset || 0;
    var max = doc.scrollHeight - doc.clientHeight;
    if (progress) progress.style.width = (max > 0 ? (scrollY / max) * 100 : 0) + "%";

    var pos = scrollY + 100; // account for sticky header
    var current = -1;
    for (var i = 0; i < sections.length; i++) {
      var s = sections[i];
      if (!s) continue;
      var top = s.getBoundingClientRect().top + scrollY;
      if (top <= pos) current = i;
    }
    links.forEach(function (a, i) { a.classList.toggle("active", i === current); });
  }

  function onScroll() {
    if (!ticking) { ticking = true; window.requestAnimationFrame(update); }
  }

  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll);
  update();

  // ---- footer year ---------------------------------------------------------
  var y = document.getElementById("year");
  if (y) y.textContent = new Date().getFullYear();
})();
