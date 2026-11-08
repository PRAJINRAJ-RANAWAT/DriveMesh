/* Landing page: scroll reveals, hand-drawn underlines, durability animation trigger.
   No libraries; everything degrades to static content without JS or with reduced motion. */
"use strict";

(() => {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // A few hand-drawn underline shapes; one is picked at random each time it is drawn.
  const SCRIBBLES = [
    "M3 14 C 50 6, 120 6, 197 11",
    "M2 12 C 40 18, 90 4, 140 10 S 185 15, 198 8",
    "M4 9 C 60 16, 110 2, 196 13",
    "M3 15 Q 60 3, 105 11 T 197 7",
    "M2 10 C 30 4, 70 18, 120 9 C 150 4, 180 14, 198 10",
  ];

  function drawScribble(el) {
    const path = el.querySelector("path");
    if (!path) return;
    path.setAttribute("d", SCRIBBLES[Math.floor(Math.random() * SCRIBBLES.length)]);
    el.classList.remove("drawn");
    void path.getBoundingClientRect(); // restart the CSS animation
    el.classList.add("drawn");
  }

  const scribbles = document.querySelectorAll("[data-scribble]");
  const reveals = document.querySelectorAll(".reveal");
  const players = document.querySelectorAll("[data-play]");

  if (reduced || !("IntersectionObserver" in window)) {
    reveals.forEach((el) => el.classList.add("in"));
    scribbles.forEach((el) => el.classList.add("drawn"));
    players.forEach((el) => { if (!reduced) el.classList.add("play"); });
    return;
  }

  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const el = e.target;
      if (el.classList.contains("reveal")) el.classList.add("in");
      if (el.hasAttribute("data-scribble")) setTimeout(() => drawScribble(el), 250);
      if (el.hasAttribute("data-play")) el.classList.add("play");
      io.unobserve(el);
    }
  }, { threshold: 0.25, rootMargin: "0px 0px -8% 0px" });

  reveals.forEach((el) => io.observe(el));
  scribbles.forEach((el) => {
    io.observe(el);
    el.addEventListener("mouseenter", () => drawScribble(el)); // redraw a new random stroke
  });
  players.forEach((el) => io.observe(el));
})();
