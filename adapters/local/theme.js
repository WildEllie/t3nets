// Theme init — runs immediately to prevent flash of wrong theme
(function () {
  var stored = localStorage.getItem("t3nets_theme");
  var theme = stored || "dark";
  document.documentElement.setAttribute("data-theme", theme);
  document.documentElement.setAttribute("data-bs-theme", theme);
})();

function toggleTheme() {
  var current = document.documentElement.getAttribute("data-theme") || "dark";
  var next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  document.documentElement.setAttribute("data-bs-theme", next);
  localStorage.setItem("t3nets_theme", next);
}
