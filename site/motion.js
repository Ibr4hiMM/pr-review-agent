// Public site: playful motion. Pop-in reveals, a hero that marks "proves" in red pen and turns it green,
// a red-to-green scroll bar, squishy buttons, confetti on copy and a tilting screenshot.
// The page only sets .motion when reduced motion is off, and this script does nothing without it.
(function () {
  "use strict";
  const root = document.documentElement;
  if (!root.classList.contains("motion")) return;
  root.classList.add("motion-ready");

  // Hero title: one span per word, so the words can pop in one after another.
  const title = document.querySelector(".hero-title");
  const words = title.textContent.trim().split(/\s+/);
  title.setAttribute("aria-label", title.textContent.trim());
  title.textContent = "";
  words.forEach((word, i) => {
    const w = document.createElement("span");
    w.className = "w";
    w.setAttribute("aria-hidden", "true");
    w.style.setProperty("--i", i);
    w.style.setProperty("--tilt", i % 2 ? 1 : -1);
    if (word === "proves") {
      w.classList.add("pen-word");
      w.style.setProperty("--pen-delay", `${words.length * 70 + 350}ms`);
      w.innerHTML = `${word}<svg viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true">` +
        `<path pathLength="1" d="M2 6 C12 1, 20 9, 30 5 S48 1, 58 5 S76 9, 86 4 S96 3, 98 5"/></svg>`;
    } else {
      w.textContent = word;
    }
    title.append(w, i < words.length - 1 ? " " : "");
  });
  const pen = title.querySelector(".pen-word path");
  if (pen) {
    pen.addEventListener("transitionend", (e) => {
      if (e.propertyName === "stroke-dashoffset") pen.parentNode.parentNode.classList.add("is-proven");
    });
  }

  // Sections: stagger their heading, lead and items, and pop them in once they scroll into view.
  const groups = [".site-h2", ".site-lead", ".proof-kinds > li", ".shot", ".can-do > li", ".safety > li", ".s-cmd", "#pipeline"];
  for (const section of document.querySelectorAll(".site-section")) {
    let i = 0;
    for (const el of section.querySelectorAll(groups.join(","))) {
      el.classList.add("rv");
      el.style.setProperty("--i", i++);
      // Hand the element back to its normal hover transitions once it has landed.
      el.addEventListener("transitionend", function done(e) {
        if (e.target !== el || e.propertyName !== "transform") return;
        el.classList.remove("rv");
        el.removeEventListener("transitionend", done);
      });
    }
  }
  const hero = document.querySelector(".site-hero");
  ["hero-lead", "hero-actions"].forEach((cls, n) => hero.querySelector(`.${cls}`).style.setProperty("--i", words.length * 0.6 + n));
  document.getElementById("proof").style.setProperty("--i", words.length * 0.6 + 2);

  const seen = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      entry.target.classList.add("is-in");
      seen.unobserve(entry.target);
    }
  }, { threshold: 0.12, rootMargin: "0px 0px -8% 0px" });
  requestAnimationFrame(() => requestAnimationFrame(() => hero.classList.add("is-in")));
  document.querySelectorAll(".site-section").forEach((s) => seen.observe(s));

  // Scroll progress: a pen line across the top that goes from red to green.
  const bar = document.createElement("div");
  bar.className = "scroll-pen";
  bar.setAttribute("aria-hidden", "true");
  document.body.prepend(bar);
  let queued = false;
  const paint = () => {
    queued = false;
    const max = root.scrollHeight - root.clientHeight;
    bar.style.setProperty("--p", max > 0 ? Math.min(1, root.scrollTop / max) : 0);
  };
  addEventListener("scroll", () => { if (!queued) { queued = true; requestAnimationFrame(paint); } }, { passive: true });
  paint();

  // Copy buttons: a jelly squish and a burst of red, amber and green confetti.
  const colors = ["var(--red)", "var(--amber)", "var(--green)"];
  for (const button of document.querySelectorAll("[data-copy]")) {
    button.addEventListener("click", () => {
      button.classList.remove("is-copied");
      void button.offsetWidth; // restart the animation on repeat clicks
      button.classList.add("is-copied");
      const box = button.getBoundingClientRect();
      for (let n = 0; n < 14; n++) {
        const bit = document.createElement("span");
        const angle = (n / 14) * Math.PI * 2 + Math.random() * 0.4;
        const dist = 34 + Math.random() * 30;
        bit.className = "confetti";
        bit.style.left = `${box.left + box.width / 2}px`;
        bit.style.top = `${box.top + box.height / 2}px`;
        bit.style.background = colors[n % colors.length];
        bit.style.setProperty("--dx", `${Math.cos(angle) * dist}px`);
        bit.style.setProperty("--dy", `${Math.sin(angle) * dist - 12}px`);
        bit.style.setProperty("--r", `${Math.random() * 540 - 270}deg`);
        bit.addEventListener("animationend", () => bit.remove());
        document.body.append(bit);
      }
    });
  }

  // Screenshot: leans toward the pointer, only where there is a real pointer.
  const shot = document.querySelector(".shot");
  if (shot && matchMedia("(hover: hover) and (pointer: fine)").matches) {
    const img = shot.querySelector("img");
    img.addEventListener("pointermove", (e) => {
      const r = img.getBoundingClientRect();
      const x = (e.clientX - r.left) / r.width - 0.5;
      const y = (e.clientY - r.top) / r.height - 0.5;
      shot.style.setProperty("--ry", `${x * 5}deg`);
      shot.style.setProperty("--rx", `${-y * 4}deg`);
      shot.classList.add("is-tilting");
    });
    img.addEventListener("pointerleave", () => shot.classList.remove("is-tilting"));
  }
})();
