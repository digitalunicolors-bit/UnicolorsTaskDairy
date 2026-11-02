// Light and dark mode. The new theme spreads out from the switch like paint.
(function () {
  const root = document.documentElement;
  const COLORS = { dark: "#0B1220", light: "#F5F6F9" };

  function sync() {
    const dark = root.dataset.theme !== "light";
    document.querySelectorAll("[data-theme-toggle]").forEach(el => {
      el.setAttribute("aria-checked", dark ? "true" : "false");
      el.title = dark ? "Switch to light mode" : "Switch to dark mode";
    });
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = COLORS[dark ? "dark" : "light"];
  }

  function apply(theme) {
    root.dataset.theme = theme;
    try { localStorage.setItem("theme", theme); } catch (e) {}
    sync();
  }

  function toggle(button) {
    const next = root.dataset.theme === "light" ? "dark" : "light";
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (still || !document.startViewTransition) { apply(next); return; }

    const box = button.getBoundingClientRect();
    const x = box.left + box.width / 2, y = box.top + box.height / 2;
    const reach = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
    document.startViewTransition(() => apply(next)).ready.then(() => {
      root.animate(
        { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${reach}px at ${x}px ${y}px)`] },
        { duration: 520, easing: "cubic-bezier(.3, .7, .2, 1)", pseudoElement: "::view-transition-new(root)" }
      );
    }).catch(() => {});
  }

  document.addEventListener("click", e => {
    const button = e.target.closest("[data-theme-toggle]");
    if (button) toggle(button);
  });
  sync();
})();
