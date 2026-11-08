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

/* ---- story, noise wipe, count-up -------------------------------------------------------- */
(() => {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const clamp = (v) => Math.max(0, Math.min(1, v));
  const lerp = (a, b, t) => a + (b - a) * t;
  const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);
  const STROKES = ["M3 14 C 50 6, 120 6, 197 11", "M2 12 C 40 18, 90 4, 140 10 S 185 15, 198 8", "M4 9 C 60 16, 110 2, 196 13", "M3 15 Q 60 3, 105 11 T 197 7"];
  function draw(el) {
    const p = $("path", el);
    if (!p) return;
    p.setAttribute("d", STROKES[Math.floor(Math.random() * STROKES.length)]);
    el.classList.remove("drawn"); void p.getBoundingClientRect(); el.classList.add("drawn");
  }

  /* -- story: Split. Encrypt. Replicate. Relax. -- */
  const story = $("[data-story]");
  const CAPTIONS = [
    "A 32 MB file becomes four 8 MiB chunks, streamed through. It's never held whole in memory.",
    "Each chunk is sealed with AES-256-GCM and gets a random name. Google only ever stores noise.",
    "Every chunk is copied to two different accounts, picked by free space and current load.",
    "That's it. Lose an account and DriveMesh rebuilds the missing copies on its own.",
  ];
  if (story) {
    const words = $$(".story-words li", story);
    const caption = $("#story-caption");
    const bar = $("#story-bar");
    const chunks = $$(".st-chunk", story);
    const copies = $$(".st-copy", story);
    const fileBody = $(".st-file-body", story);
    const ROWS = [175, 210, 245, 280], TARGET_Y = [105, 175, 245, 315];
    const ACC_Y = [30, 112, 194, 276, 358];
    // copy -> [chunk, account, slot]; matches the dashed links in the SVG
    const PLAN = [[0, 0, 0], [0, 1, 0], [1, 2, 0], [1, 3, 0], [2, 4, 0], [2, 0, 1], [3, 1, 1], [3, 2, 1]];
    let phase = -1;

    function setPhase(p) {
      if (p === phase) return;
      phase = p;
      story.dataset.phase = String(p);
      words.forEach((li, i) => { li.classList.toggle("on", i === p); li.classList.toggle("past", i < p); });
      caption.textContent = CAPTIONS[p];
      const sc = $("[data-story-scribble]", words[p]);
      if (sc) setTimeout(() => draw(sc), 120);
    }
    function render(prog) {
      setPhase(prog < 0.25 ? 0 : prog < 0.5 ? 1 : prog < 0.78 ? 2 : 3);
      bar.style.width = (prog * 100).toFixed(1) + "%";
      const ts = ease(clamp((prog - 0.03) / 0.17));
      fileBody.style.opacity = String(lerp(1, 0.35, ts));
      chunks.forEach((g, i) => {
        const t = ease(clamp((prog - 0.03 - i * 0.02) / 0.15));
        g.setAttribute("transform", `translate(${lerp(45, 225, t).toFixed(1)} ${lerp(ROWS[i], TARGET_Y[i], t).toFixed(1)}) scale(1 ${lerp(0.65, 1, t).toFixed(3)})`);
      });
      copies.forEach((r, k) => {
        const [c, a, slot] = PLAN[k];
        const t = ease(clamp((prog - 0.52 - k * 0.022) / 0.14));
        const x0 = 278, y0 = TARGET_Y[c] + 13, x1 = 412 + slot * 18, y1 = ACC_Y[a] + 32;
        r.setAttribute("x", lerp(x0, x1, t).toFixed(1));
        r.setAttribute("y", lerp(y0, y1, t).toFixed(1));
        r.style.opacity = prog > 0.5 ? "1" : "0";
      });
    }
    if (reduced) {
      render(1);
      words.forEach((li) => li.classList.add("on"));
      $$("[data-story-scribble]", story).forEach((el) => el.classList.add("drawn"));
    } else {
      let ticking = false;
      const update = () => {
        ticking = false;
        const r = story.getBoundingClientRect();
        const total = r.height - window.innerHeight;
        render(total > 0 ? clamp(-r.top / total) : 1);
      };
      window.addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(update); } }, { passive: true });
      window.addEventListener("resize", update);
      update();
    }
  }

  /* -- noise wipe: what you see vs what Google sees -- */
  const wipe = $("[data-wipe]");
  const canvas = $("#noise-canvas");
  if (wipe && canvas) {
    const paint = () => {
      const css = getComputedStyle(document.documentElement);
      const ink = css.getPropertyValue("--color-on-surface-muted").trim() || "#A8A39A";
      const accent = css.getPropertyValue("--color-accent").trim() || "#FF5A1F";
      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (let y = 0; y < canvas.height; y++) {
        for (let x = 0; x < canvas.width; x++) {
          const r = Math.random();
          if (r < 0.45) continue;
          ctx.globalAlpha = 0.15 + Math.random() * 0.75;
          ctx.fillStyle = r > 0.985 ? accent : ink;
          ctx.fillRect(x, y, 1, 1);
        }
      }
      ctx.globalAlpha = 1;
    };
    paint();
    new MutationObserver(paint).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const layer = $("#wipe-noise"), handle = $("#wipe-handle");
    const apply = (q) => {
      const left = (100 - q * 100).toFixed(1);
      layer.style.clipPath = `inset(0 0 0 ${left}%)`;
      handle.style.left = `${left}%`;
    };
    if (reduced) { apply(0.5); } else {
      let ticking = false;
      const update = () => {
        ticking = false;
        const r = wipe.getBoundingClientRect();
        const vh = window.innerHeight;
        apply(clamp((vh * 0.85 - r.top) / (r.height + vh * 0.35)) * 0.92 + 0.04);
      };
      window.addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(update); } }, { passive: true });
      update();
    }
  }

  /* -- count-up numbers -- */
  const counters = $$("[data-count]");
  if (!reduced && "IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        io.unobserve(e.target);
        const el = e.target, end = parseInt(el.dataset.count, 10), t0 = performance.now(), dur = 1200;
        if (!Number.isFinite(end)) continue;
        const step = (now) => {
          const t = clamp((now - t0) / dur);
          el.textContent = String(Math.round(end * (1 - Math.pow(1 - t, 3))));
          if (t < 1) requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
      }
    }, { threshold: 0.6 });
    counters.forEach((el) => io.observe(el));
  }
})();
