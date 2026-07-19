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

  document.querySelectorAll(".shot img").forEach(function (img) {
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

  // ---- footer year ---------------------------------------------------------
  var y = document.getElementById("year");
  if (y) y.textContent = new Date().getFullYear();
})();
