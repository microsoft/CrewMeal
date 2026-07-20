(function () {
  "use strict";

  // ---- language toggle -----------------------------------------------------
  var STORAGE_KEY = "crewmeal-lang";
  var buttons = document.querySelectorAll(".lang button");

  function apply(lang) {
    lang = lang === "en" ? "en" : "ko";
    document.documentElement.setAttribute("lang", lang);
    document.querySelectorAll("[data-ko]").forEach(function (el) {
      var val = el.getAttribute("data-" + lang);
      if (val === null) return;
      if (el.hasAttribute("data-i18n-html")) {
        el.innerHTML = val;
      } else {
        el.textContent = val;
      }
    });
    buttons.forEach(function (b) {
      var on = b.dataset.lang === lang;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
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
      box.setAttribute("aria-hidden", "false");
    });
  });
  function closeBox() {
    if (!box) return;
    box.classList.remove("open");
    box.setAttribute("aria-hidden", "true");
  }
  if (box) {
    box.addEventListener("click", closeBox);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeBox();
    });
  }

  // ---- scroll progress -----------------------------------------------------
  var progress = document.getElementById("progress");
  var ticking = false;

  function update() {
    ticking = false;
    var doc = document.documentElement;
    var scrollY = window.scrollY || window.pageYOffset || 0;
    var max = doc.scrollHeight - doc.clientHeight;
    if (progress) progress.style.width = (max > 0 ? (scrollY / max) * 100 : 0) + "%";
  }
  function onScroll() {
    if (!ticking) { ticking = true; window.requestAnimationFrame(update); }
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll);
  update();

  // ---- reveal on scroll ----------------------------------------------------
  var reveals = document.querySelectorAll("[data-reveal]");
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  if (reduce || !("IntersectionObserver" in window)) {
    reveals.forEach(function (el) { el.classList.add("is-in"); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-in");
          io.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    reveals.forEach(function (el) { io.observe(el); });
  }

  // ---- footer year ---------------------------------------------------------
  var y = document.getElementById("year");
  if (y) y.textContent = new Date().getFullYear();
})();
