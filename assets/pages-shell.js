/* Static Pages demos: no API calls. */
(function () {
  document.querySelectorAll("[data-demo-nav]").forEach((link) => {
    if (link.getAttribute("aria-current") === "page") return;
  });
})();
