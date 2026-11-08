/* Landing page behaviour: nav state, staggered reveals, hand-drawn underlines, the scroll-driven
   "account dies" scene and the noise→file punchline. Vanilla JS, no libraries.
   Everything degrades to a complete static page without JS or under prefers-reduced-motion. */
"use strict";

(() => {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];

  /* ---- navbar: translucent + blur once the page scrolls ---- */
  const nav = $("#site-nav");
  const darkBands = $$(".dark, .cta-band");
  const onScrollNav = () => {
    nav.classList.toggle("scrolled", window.scrollY > 8);
    const probe = nav.offsetHeight / 2;  // is a dark band under the middle of the navbar?
    nav.classList.toggle("over-dark", darkBands.some((el) => {
      const r = el.getBoundingClientRect();
      return el.classList.contains("dark") && r.top <= probe && r.bottom >= probe;
    }));
  };
  onScrollNav();
  window.addEventListener("scroll", onScrollNav, { passive: true });

  /* ---- hand-drawn underline: a random stroke each time it is drawn ---- */
  const STROKES = [
    "M3 14 C 50 6, 120 6, 197 11",
    "M2 12 C 40 18, 90 4, 140 10 S 185 15, 198 8",
    "M4 9 C 60 16, 110 2, 196 13",
    "M3 15 Q 60 3, 105 11 T 197 7",
    "M2 10 C 30 4, 70 18, 120 9 C 150 4, 180 14, 198 10",
  ];
  function draw(el) {
    const path = $("path", el);
    if (!path) return;
    path.setAttribute("d", STROKES[Math.floor(Math.random() * STROKES.length)]);
    el.classList.remove("drawn");
    void path.getBoundingClientRect();
    el.classList.add("drawn");
  }

  /* ---- "Google sees noise": random ciphertext-like cells ---- */
  const cells = $("#noise-cells");
  if (cells) {
    const NS = "http://www.w3.org/2000/svg";
    for (let y = 0; y < 8; y++) {
      for (let x = 0; x < 12; x++) {
        if (Math.random() < 0.55) {
          const r = document.createElementNS(NS, "rect");
          r.setAttribute("x", String(x * 8)); r.setAttribute("y", String(y * 8));
          r.setAttribute("width", "6"); r.setAttribute("height", "6"); r.setAttribute("rx", "1");
          r.setAttribute("opacity", (0.25 + Math.random() * 0.75).toFixed(2));
          cells.append(r);
        }
      }
    }
  }

  const reveals = $$(".reveal");
  const scribbles = $$("[data-scribble]");
  if (reduced || !("IntersectionObserver" in window)) {
    reveals.forEach((el) => el.classList.add("in"));
    scribbles.forEach((el) => el.classList.add("drawn"));
  } else {
    // stagger siblings that enter together
    const groups = new Map();
    reveals.forEach((el) => {
      const key = el.parentElement;
      const i = groups.get(key) || 0;
      el.style.setProperty("--delay", `${i * 110}ms`);
      groups.set(key, i + 1);
    });
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        if (e.target.classList.contains("reveal")) e.target.classList.add("in");
        if (e.target.hasAttribute("data-scribble")) setTimeout(() => draw(e.target), 300);
        io.unobserve(e.target);
      }
    }, { threshold: 0.2, rootMargin: "0px 0px -6% 0px" });
    reveals.forEach((el) => io.observe(el));
    scribbles.forEach((el) => { io.observe(el); el.addEventListener("mouseenter", () => draw(el)); });
  }

  /* ---- scroll-driven durability scene ---- */
  const scene = $("[data-scene]");
  if (!scene) return;
  const steps = $$(".scene-steps li", scene);
  const status = $("#sv-status");
  const fly1 = $("#fly-1"), fly2 = $("#fly-2");
  const sceneScribble = $("[data-scribble-scene]", scene);
  const STATUS = [
    "All accounts healthy · every chunk on 2 accounts",
    "Account D vanished · 2 chunks down to 1 copy",
    "Repairing · re-copying the endangered chunks",
    "Healed · every chunk back on 2 accounts",
  ];
  // packet paths (SVG user units): A's chunk → C, C's chunk → B
  const PATH1 = { dx: 246, dy: 170 }, PATH2 = { dx: 26, dy: -170 };
  let phase = -1, scribbled = false;

  function setPhase(p) {
    if (p === phase) return;
    phase = p;
    scene.dataset.phase = String(p);
    steps.forEach((li, i) => li.classList.toggle("on", reduced ? true : i === p));
    status.textContent = STATUS[p];
    if (p === 3 && sceneScribble && !scribbled) { scribbled = true; setTimeout(() => draw(sceneScribble), 250); }
  }

  if (reduced) { setPhase(3); return; }

  const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);
  let ticking = false;
  function update() {
    ticking = false;
    const r = scene.getBoundingClientRect();
    const total = r.height - window.innerHeight;
    const prog = total > 0 ? Math.min(1, Math.max(0, -r.top / total)) : 1;
    setPhase(prog < 0.22 ? 0 : prog < 0.42 ? 1 : prog < 0.72 ? 2 : 3);
    const t = ease(Math.min(1, Math.max(0, (prog - 0.42) / 0.3)));
    fly1.setAttribute("transform", `translate(${(PATH1.dx * t).toFixed(1)} ${(PATH1.dy * t).toFixed(1)})`);
    fly2.setAttribute("transform", `translate(${(PATH2.dx * t).toFixed(1)} ${(PATH2.dy * t).toFixed(1)})`);
  }
  const onScroll = () => { if (!ticking) { ticking = true; requestAnimationFrame(update); } };
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll);
  update();
})();
