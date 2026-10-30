// Applies the stored theme choice before first paint (decision 85, FR-157). It is loaded by a plain
// script tag, first in the head, so it runs before the body is parsed. It makes no request, and a
// throw (a private window, blocked site data) or an unknown value leaves the system scheme in force.
try {
  var choice = window.localStorage.getItem("harnessed.theme");
  if (choice === "light" || choice === "dark") {
    document.documentElement.dataset.theme = choice;
    // The toggle is not parsed yet: once it is, name its action from the labels the server HTML carries
    // (FR-174), so a stored choice never shows the wrong label before hydration.
    var label = function () {
      var toggle = document.querySelector("button[data-label-" + choice + "]");
      var text = toggle && toggle.getAttribute("data-label-" + choice);
      if (toggle && text) {
        toggle.setAttribute("aria-label", text);
        toggle.setAttribute("title", text);
      }
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", label);
    else label();
  }
} catch {
  // Nothing stored and nothing to apply: the page follows prefers-color-scheme.
}
