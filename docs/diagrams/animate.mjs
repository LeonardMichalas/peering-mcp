// Turn a rendered diagram into a pair of animated SVGs for the README.
//
// GitHub strips inline <svg> and <video> from markdown. The one thing that
// animates is an SVG referenced as an image: the browser loads it in the SVG
// spec's "secure animated mode", where scripting, external references and
// interactivity are off and declarative animation is the only thing still on.
//
// So the motion here is SMIL, and it reuses geometry rather than restating it.
// Every edge is already a path string; <animateMotion path="..."> takes that
// same string, so a dot travels the exact route the edge draws. Each dot starts
// at a negative offset, which means the animation is already in its steady
// state on the first frame instead of lying about it for a few seconds.
//
// An image cannot see the theme of the page it lands in, so this emits one file
// per theme and the README pairs them with <picture>.
//
// Usage:
//   node docs/diagrams/animate.mjs <rendered.html> <out-basename>
// Writes <out-basename>-light.svg and <out-basename>-dark.svg.

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const [source, outBase] = process.argv.slice(2);
if (!source || !outBase) {
  console.error('usage: node docs/diagrams/animate.mjs <rendered.html> <out-basename>');
  process.exit(2);
}

const CHROME = ['chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable']
  .map((bin) => (process.env.PATH || '').split(path.delimiter).map((dir) => path.join(dir, bin)))
  .flat()
  .find((candidate) => fs.existsSync(candidate));
if (!CHROME) {
  console.error('no Chromium-family browser found; one is needed to resolve the page CSS');
  process.exit(1);
}

/** Runs inside the page, where the real CSS and path geometry are available. */
function buildScript(theme) {
  return `
<script>
(function () {
  document.documentElement.setAttribute('data-theme', ${JSON.stringify(theme)});
  const SVGNS = 'http://www.w3.org/2000/svg';
  const svg = document.querySelector('svg');

  // Viewer-only scaffolding is interaction, and interaction is off in an image.
  svg.querySelectorAll('[class*="relationship-"], [class*="hit-target"]').forEach((n) => n.remove());

  // Resolve the stylesheet onto the elements. The result needs no CSS of its
  // own, which is the only way it survives being loaded as a bare image.
  const PROPS = ['fill', 'fill-opacity', 'stroke', 'stroke-width', 'stroke-opacity',
    'stroke-dasharray', 'stroke-linecap', 'stroke-linejoin', 'opacity', 'font-family',
    'font-size', 'font-weight', 'font-style', 'letter-spacing', 'text-anchor',
    'dominant-baseline', 'display'];
  for (const el of [svg, ...svg.querySelectorAll('*')]) {
    const cs = getComputedStyle(el);
    el.setAttribute('style', PROPS.map((p) => p + ':' + cs.getPropertyValue(p)).join(';'));
  }

  const view = (svg.getAttribute('viewBox') || '0 0 1080 545').split(/\\s+/).map(Number);
  const [vx, vy, vw, vh] = view;

  // The page background is not part of the SVG. Without this a dark diagram
  // renders on whatever ground the host page happens to have.
  const bg = document.createElementNS(SVGNS, 'rect');
  bg.setAttribute('x', vx); bg.setAttribute('y', vy);
  bg.setAttribute('width', vw); bg.setAttribute('height', vh);
  bg.setAttribute('style', 'fill:' + getComputedStyle(document.body).backgroundColor);
  svg.insertBefore(bg, svg.firstChild);

  // Edges carry an a-* class. In a sequence diagram so do the lifelines, and a
  // dot running down a lifeline would describe something that never happens.
  const isLifeline = (p) => {
    const len = p.getTotalLength();
    const a = p.getPointAtLength(0), b = p.getPointAtLength(len);
    return Math.abs(b.y - a.y) > Math.abs(b.x - a.x) && Math.abs(b.y - a.y) > vh * 0.5;
  };

  const edges = [...svg.querySelectorAll('path.a-default, path.a-emphasis, path.a-dashed')]
    .filter((p) => p.getTotalLength() > 24 && !isLifeline(p));

  const CYCLE = 4.8;
  edges.forEach((edge, i) => {
    const d = edge.getAttribute('d');
    if (!d) return;
    const dot = document.createElementNS(SVGNS, 'circle');
    dot.setAttribute('r', '4');
    dot.setAttribute('style', 'fill:' + getComputedStyle(edge).stroke);
    const motion = document.createElementNS(SVGNS, 'animateMotion');
    motion.setAttribute('dur', CYCLE + 's');
    motion.setAttribute('repeatCount', 'indefinite');
    motion.setAttribute('path', d);
    motion.setAttribute('keyPoints', '0;1;1');
    motion.setAttribute('keyTimes', '0;' + (1 / edges.length).toFixed(4) + ';1');
    motion.setAttribute('calcMode', 'linear');
    // Negative, so the first painted frame is already the steady state.
    motion.setAttribute('begin', '-' + (CYCLE - (i * CYCLE) / edges.length).toFixed(3) + 's');
    dot.appendChild(motion);
    // Same parent as the edge, so it inherits the same ancestor transforms.
    edge.parentNode.insertBefore(dot, edge.nextSibling);
  });

  // Carry the embedded font faces across. They are data URIs, so this stays
  // within "no external references" while keeping the text metrics exact.
  const faces = [];
  for (const sheet of document.styleSheets) {
    let rules;
    try { rules = sheet.cssRules; } catch { continue; }
    for (const rule of rules) if (rule.constructor.name === 'CSSFontFaceRule') faces.push(rule.cssText);
  }
  const style = document.createElementNS(SVGNS, 'style');
  style.textContent = faces.join('\\n') +
    '\\n@media (prefers-reduced-motion: reduce){circle[r="4"]{display:none}}';
  svg.insertBefore(style, svg.firstChild);

  svg.setAttribute('xmlns', SVGNS);
  svg.setAttribute('width', vw);
  svg.setAttribute('height', vh);

  const out = document.createElement('pre');
  out.id = 'archify-out';
  out.textContent = svg.outerHTML;
  document.body.replaceChildren(out);
})();
</script>
`;
}

function render(theme, outFile) {
  const probe = path.join(os.tmpdir(), `archify-probe-${theme}-${process.pid}.html`);
  const html = fs.readFileSync(source, 'utf8').replace('</body>', buildScript(theme) + '</body>');
  fs.writeFileSync(probe, html);
  try {
    const dump = execFileSync(CHROME, ['--headless', '--disable-gpu', '--no-sandbox',
      '--virtual-time-budget=5000', '--dump-dom', probe],
      { maxBuffer: 128 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] }).toString();
    const found = dump.match(/<pre id="archify-out">([\s\S]*?)<\/pre>/);
    if (!found) throw new Error('the page did not produce an SVG');
    const svg = '<?xml version="1.0" encoding="UTF-8"?>\n' + found[1]
      .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'").replace(/&amp;/g, '&') + '\n';
    fs.writeFileSync(outFile, svg);
    const dots = (svg.match(/<animateMotion/g) || []).length;
    console.log(`${outFile}  ${(Buffer.byteLength(svg) / 1024).toFixed(0)} KB  ${dots} animated edges`);
  } finally {
    fs.rmSync(probe, { force: true });
  }
}

render('light', `${outBase}-light.svg`);
render('dark', `${outBase}-dark.svg`);
