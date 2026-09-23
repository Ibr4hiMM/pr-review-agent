// Public site: mounts the shared animations into the static page and wires the copy buttons.
(function () {
  "use strict";
  const L = window.PRLanding;
  const story = L.storyFromFinding(L.DEMO.finding, L.DEMO.source);
  L.mountProof(document.getElementById("proof"), story, {
    caption: "A seeded bug in a small test repository, reviewed by pr-review.",
  });

  const host = document.getElementById("pipeline");
  let pipe = L.mountPipeline(host);
  const narrow = window.matchMedia("(max-width: 720px)");
  narrow.addEventListener("change", () => {
    pipe.destroy();
    pipe = L.mountPipeline(host);
  });

  for (const button of document.querySelectorAll("[data-copy]")) {
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(button.dataset.copy);
        button.textContent = "Copied";
      } catch {
        button.textContent = "Select and copy";
      }
      setTimeout(() => { button.textContent = "Copy"; }, 1600);
    });
  }
})();
