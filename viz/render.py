"""Render a timeline to one self-contained HTML file.

No external assets and no network: the data is embedded as JSON and the
page is a few hundred lines of plain JavaScript, so the file works from a
local path, over email, and offline.

**Two channels, borrowed logic, own palette.** Border carries what is
known about the statement and fill carries what is known about the body,
which is how a blueprint reads, so a Lean reader needs no second
convention. The colours are not a blueprint's and the page says so: this
renders a Choir project's history, not a blueprint, and should not be
mistaken for one.

**The page says what it measured.** A node is clear when the prover's
placeholder token is gone from its body and from every dependency the
roadmap declares — a pattern match over the source, not a kernel check,
against an edge list the roadmap may not have recorded. The word for the
token comes from the prover's own profile, so a Rocq project reads
`Admitted` where a Lean one reads `sorry`.

**Positions are recomputed per frame, from the subgraph that exists at
that step.** A fixed layout needs the future, which rules out watching a
project that is still running, and it draws early frames in coordinates
the structure had not yet earned. Motion is kept followable rather than
eliminated: within a layer, nodes hold the order they first appeared in,
so growth spreads outward instead of reshuffling, and each box eases to
its new place. `layout.place` is still used, for one thing: a depth per
declaration, so events sharing a timestamp read root-first.

**Which end of the tree sits on top is a toggle**, because it depends on
how the work ran rather than on a drawing convention: decomposition grows
downward from the goal, construction grows downward from the foundations.
Both read top-down, so the toggle flips the tree and the arrowhead always
sits at the lower end of an edge.

**The colours are computed, not chosen.** A live claim takes the whole box
in its holder's hue, from a pool assigned on first claim and held for the
whole timeline — colour follows the worker, never its rank in the current
frame. Depth of fill is how far the body has got, so a node half-done in a
worker's hands reads like one half-done in nobody's, in that worker's hue.
The holder keeps the box until the node and everything under it is clear,
so a worker that splits its task still visibly owns the split.

The pool holds **four** hues, which is what the reserved colours leave
room for: blue means clear, red means retracted, grey means unpublished,
and a search over the hue circle found no fifth hue that clears ΔE 15
normal-vision and ΔE 8 under deuteranopia and protanopia against those
three and against the other slots, in both modes. Cutting the pool is the
method's own answer to an oversubscribed palette. It cycles past four,
which is a deliberate departure from never cycling a categorical scale: a
long project has arbitrarily many sessions, and folding them into one
bucket would erase the distinction this view exists to show. The session
tag in the tooltip and the roster is what keeps identity off colour alone.
A hue carries its own label ink, because one ink per theme left most of
the fills failing their own label's contrast. Retraction is a reserved
status colour and always ships with a mark.
"""

from __future__ import annotations

import html
import json
from dataclasses import asdict

from gate.provers import PROFILES
from viz.layout import place
from viz.model import KIND_RANK, Event, EventKind, Node, Timeline

_TEMPLATE = """<!DOCTYPE html>
<html lang="en" data-theme="auto">
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  :root {
    color-scheme: light;
    --surface-1: #fcfcfb; --plane: #f9f9f7;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
    --grid: #e1e0d9; --border: rgba(11,11,11,0.10);
    --line: #5756527a; --line-strong: #0b0b0b;
    --ready: #2f6fed; --fill-clear: #2f6fed; --on-clear: #ffffff;
    --critical: #d03b3b;
    /* Four worker slots, not six. A six-hue pool cannot also keep clear
       of the three reserved colours — blue for clear, red for retracted,
       grey for unpublished — and a search over the hue circle found no
       sixth or fifth hue that does. Cutting the pool is the honest answer:
       these four clear ΔE 15 normal-vision and ΔE 8 under deuteranopia and
       protanopia against each other and against all three reserved
       colours, in both modes, with the same hue in each mode so flipping
       the theme never repaints a worker.
       `-ink` is per hue because one ink per mode could not work: eight of
       the twelve old fills failed their own label's contrast.
       `-soft` is the hue mixed 38% toward the surface, which is the
       proof-progress step. Two soft fills can sit under ΔE 15 of each
       other; each one is drawn inside its own solid border, which is the
       full-chroma hue, so the soft step never carries identity alone. */
    --w1: #fa8054; --w1-ink: #0b0b0b; --w1-soft: #ffcfbd;
    --w2: #01c688; --w2-ink: #0b0b0b; --w2-soft: #b8e9cf;
    --w3: #9e9bfa; --w3-ink: #0b0b0b; --w3-soft: #d6d8fd;
    --w4: #be3aac; --w4-ink: #ffffff; --w4-soft: #e9b6de;
    --ready-soft: #adc9fa; --muted-soft: #cfcecb;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --surface-1: #1a1a19; --plane: #0d0d0d;
      --text-primary: #fff; --text-secondary: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
      --line: #c9c8bd80; --line-strong: #ffffff;
      --ready: #5b8def; --fill-clear: #5b8def; --on-clear: #0b0b0b;
      --critical: #d03b3b;
      --w1: #fa8054; --w1-ink: #0b0b0b; --w1-soft: #673f30;
      --w2: #01c688; --w2-ink: #0b0b0b; --w2-soft: #295540;
      --w3: #644ddd; --w3-ink: #ffffff; --w3-soft: #33305d;
      --w4: #cf4bbc; --w4-ink: #0b0b0b; --w4-soft: #5a3051;
      --ready-soft: #324363; --muted-soft: #41403d;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface-1: #1a1a19; --plane: #0d0d0d;
    --text-primary: #fff; --text-secondary: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
    --line: #c9c8bd80; --line-strong: #ffffff;
    --ready: #5b8def; --fill-clear: #5b8def; --on-clear: #0b0b0b;
    --critical: #d03b3b;
    --w1: #fa8054; --w1-ink: #0b0b0b; --w1-soft: #673f30;
    --w2: #01c688; --w2-ink: #0b0b0b; --w2-soft: #295540;
    --w3: #644ddd; --w3-ink: #ffffff; --w3-soft: #33305d;
    --w4: #cf4bbc; --w4-ink: #0b0b0b; --w4-soft: #5a3051;
    --ready-soft: #324363; --muted-soft: #41403d;
  }
  body { margin: 0; background: var(--plane); }
  .viz-root {
    font: 13px/1.5 ui-sans-serif, system-ui, -apple-system, sans-serif;
    color: var(--text-primary); background: var(--plane);
    min-height: 100vh; padding: 20px 24px 0;
    font-variant-numeric: tabular-nums;
  }
  h1 { font-size: 15px; font-weight: 600; margin: 0 0 2px; }
  .sub { color: var(--text-secondary); margin: 0 0 12px; }
  .bar { display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
         margin-bottom: 12px; }
  .bar input[type=range] { flex: 1 1 320px; min-width: 240px;
                           accent-color: var(--ready); }
  button { font: inherit; color: var(--text-primary); background: var(--surface-1);
           border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px;
           cursor: pointer; }
  #find { font: inherit; color: var(--text-primary); background: var(--surface-1);
          border: 1px solid var(--border); border-radius: 6px; padding: 4px 8px;
          flex: 0 0 190px; min-width: 190px; }
  #find:focus { outline: 2px solid var(--ready); outline-offset: -1px; }
  /* Its own line. In the control row this grew and shrank with every
     step, which shifted every button beside it. */
  .readout { color: var(--text-secondary); margin: 0 0 10px;
             font-variant-numeric: tabular-nums; }
  .when { color: var(--text-secondary); }
  .legend { display: flex; gap: 16px; flex-wrap: wrap; align-items: center;
            color: var(--text-secondary); margin-bottom: 10px; }
  .key { display: inline-flex; gap: 6px; align-items: center; }
  .sw { width: 11px; height: 11px; border-radius: 3px; display: inline-block;
        box-sizing: border-box; }
  details.roster { color: var(--text-secondary); margin: 0 0 10px; }
  details.roster summary { cursor: pointer; }
  details.roster ul { margin: 6px 0 0; padding-left: 4px; list-style: none;
                      columns: 3; }
  details.roster li { margin: 3px 0; }
  .plot { background: var(--surface-1); border: 1px solid var(--border);
          border-radius: 8px; overflow: hidden; }
  /* With the table open the tree has to stay in view: hovering a row
     lights a box, and a box scrolled off the top of the window lights
     nothing the reader can see. */
  body.tabling .plot { position: sticky; top: 0; z-index: 2;
                       box-shadow: 0 8px 16px -8px rgba(0,0,0,.28); }
  /* Pinned, the plot is what the rows scroll past, so it gives some of
     its height back for them to scroll in. */
  body.tabling svg { height: 50vh; }
  svg { display: block; width: 100%; height: 68vh; }
  .edge { stroke: var(--line); stroke-width: 1.5; fill: none;
          transition: d .3s ease-out; }
  .edge.reduction { stroke-width: 2.2; }
  /* Last of the three, so it wins on source order. Hovering emphasises
     the edges whether or not the dimming is on: they are thin and the
     tree gets dense, so picking one out is the main reason to hover. */
  .edge.lit { stroke: var(--line-strong); stroke-width: 2.6; }
  #ar path { fill: var(--line); }
  #arLit path { fill: var(--line-strong); }
  .n { transition: transform .3s ease-out; }
  .n.pinned { cursor: grabbing; }
  .n.pinned rect { stroke-width: 3.5; }
  svg.focusing .n { opacity: .16; }
  svg.focusing .n.lit { opacity: 1; }
  svg.focusing .edge { opacity: .1; }
  svg.focusing .edge.lit { opacity: 1; }
  .n rect { stroke-width: 2; }
  .n text { font-size: 11px; pointer-events: none; }
  .n.hidden { display: none; }
  /* Search dims the tree the way hover does, but it holds: every match
     stays lit at once, and the one the stepper is on carries the thicker
     border. Its own class rather than `focusing`, so hovering a node in
     the middle of a search does not throw the matches away. */
  svg.finding .n { opacity: .14; }
  svg.finding .n.found { opacity: 1; }
  svg.finding .edge { opacity: .08; }
  .n.found rect { stroke-dasharray: 5 3; }
  .n.current rect { stroke-width: 4.5; stroke-dasharray: none; }
  #camera { transition: transform .28s ease-out; }
  /* A pinch is a stream of events, and the easing lags behind it far
     enough that the point under the pointer visibly slides away. The
     gesture turns it off; Fit and find keep their glide. */
  #camera.nudging { transition: none; }
  /* Hovering a name in the table borrows the tree's own hover lighting.
     The box thickens and a ring is drawn around it: the ring rides inside
     the node's own group, so it follows the box wherever the layout puts
     it, and it reads at a glance in a way a stroke width alone does not. */
  .n .ring { display: none; fill: none; stroke: var(--line-strong);
             stroke-width: 2; }
  .n.cued .ring { display: block; }
  .n.cued rect:not(.ring) { stroke-width: 4.5; }
  .mark { font-size: 13px; pointer-events: none; }
  .tip { position: fixed; z-index: 9;
         background: var(--surface-1); color: var(--text-primary);
         border: 1px solid var(--border); border-radius: 6px;
         padding: 8px 10px; max-width: 380px;
         box-shadow: 0 4px 14px rgba(0,0,0,.16); display: none;
         user-select: text; }
  .tip b { font-weight: 600; word-break: break-all; }
  .tip dl { margin: 6px 0 0; display: grid; grid-template-columns: auto 1fr;
            gap: 2px 10px; }
  .tip dt { color: var(--muted); }
  .tip dd { margin: 0; word-break: break-all; }
  table { border-collapse: collapse; width: 100%; font-size: 12px;
          margin: 12px 0 24px; }
  th, td { text-align: left; padding: 4px 8px;
           border-bottom: 1px solid var(--grid); }
  th { color: var(--muted); font-weight: 500; }
  .notes { color: var(--text-secondary); margin: 10px 0 0; padding-left: 18px; }
  .notes li { margin: 2px 0; }
  #tableView { display: none; }
  #sortWhen { cursor: pointer; user-select: none; }
  #sortWhen:hover { color: var(--text-primary); }
  td.decl { cursor: default; }
  td.decl:hover { background: var(--grid); }
  /* The scrubber has not reached this row, so there is no box to light. */
  td.decl.absent { color: var(--muted); font-style: italic; }
</style>
<body>
<div class="viz-root">
  <h1>__TITLE__</h1>
  <p class="sub"><span id="counts"></span> · __SUBTITLE__</p>
  <p class="sub">This is a visualization of task management, not a
  blueprint. States are matched in the source text, never checked by the
  prover.</p>

  <div class="bar">
    <button id="play">▶ Play</button>
    <input type="range" id="scrub" min="0" max="0" value="0">
    <label class="key">speed
      <input type="range" id="speed" min="0" max="5" step="1" value="3"
             style="flex:0 0 90px;min-width:90px">
      <span id="speedLabel" class="when"
            style="display:inline-block;min-width:76px"></span>
    </label>
    <button id="fit">Fit ▸ auto</button>
    <button id="flow" title="which end of the tree sits on top; arrows always point down the page">
      flow: goal → obligations</button>
    <button id="hl" title="dim the rest of the tree while hovering a node">
      highlight: on</button>
    <button id="scope"
            title="the plan's own nodes, or every declaration in the corpus">
      scope: plan</button>
    <label class="key">find
      <input id="find" type="search" autocomplete="off" spellcheck="false"
             placeholder="declaration name"
             title="matches the full name, namespace included"></label>
    <button id="findPrev" title="previous match">&#8249;</button>
    <button id="findNext" title="next match">&#8250;</button>
    <span id="findCount" class="when"
          style="display:inline-block;min-width:72px"></span>
    <button id="toggleTable">Table view</button>
    <button id="toggleTheme">Theme</button>
  </div>

  <p class="readout" id="when"></p>
  <div class="legend" id="legend"></div>
  <details class="roster" id="roster"></details>

  <div class="plot">
    <svg id="svg">
      <defs>
        <!-- Two markers rather than one with `context-stroke`. A single
             shared marker leaves the head's colour up to how an engine
             resolves context paint, and `markerUnits="userSpaceOnUse"`
             keeps it the same size however thick the line gets — so the
             head stayed thin and pale beside a highlighted line. Swapping
             the marker changes both. -->
        <marker id="ar" viewBox="0 0 10 10" refX="9" refY="5"
                markerUnits="userSpaceOnUse" markerWidth="8" markerHeight="8"
                orient="auto-start-reverse">
          <path d="M1,2.6 L9,5 L1,7.4 z"></path>
        </marker>
        <marker id="arLit" viewBox="0 0 10 10" refX="9" refY="5"
                markerUnits="userSpaceOnUse" markerWidth="11" markerHeight="11"
                orient="auto-start-reverse">
          <path d="M1,2.4 L9,5 L1,7.6 z"></path>
        </marker>
      </defs>
      <g id="camera"><g id="edges"></g><g id="nodes"></g></g></svg>
  </div>

  <div id="tableView">
    <table id="eventTable"><thead><tr>
      <th id="sortWhen" title="click to reverse the order">when
        <span id="sortTri">&#9662;</span></th>
      <th>event</th><th>declaration</th><th>who</th><th>ref</th>
    </tr></thead><tbody></tbody></table>
  </div>

  <ul class="notes" id="notes"></ul>
</div>

<div class="tip" id="tip"></div>

<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const NS = 'http://www.w3.org/2000/svg';

// The payload carries every declaration; scope decides which are drawn.
// `plan` is the tree the project intends — a task, a roadmap group, or
// declared dependencies. `all` adds what was written while proving it,
// mostly helper lemmas the roadmap has no reason to name.
//
// The scrubber steps through the events of the current scope, but state
// replays over *every* event: a reduction's obligation can be the far end
// of an edge before the roadmap has a node for it, and a placeholder
// filled below the plan still decides whether the node above it is clear.
let scope = D.scope === 'all' ? 'all' : 'plan';
const planOf = new Map(D.nodes.map(n => [n.decl, n.plan !== false]));
const inScope = decl => scope === 'all' || planOf.get(decl) !== false;
let active = [];

function rebuildScope() {
  active = [];
  for (let i = 0; i < D.events.length; i++) {
    const d = D.events[i].decl;
    if (!d || inScope(d)) active.push(i);
  }
  scrub.max = Math.max(0, active.length - 1);
  let shown = 0, drawn = 0;
  for (const n of D.nodes) if (inScope(n.decl)) shown++;
  for (const l of links) if (inScope(l.parent) && inScope(l.child)) drawn++;
  document.getElementById('counts').textContent =
    `${shown} nodes \u00b7 ${drawn} declared edges \u00b7 ${active.length}`
    + ` events \u00b7 scope ${scope}`;
  document.getElementById('scope').textContent = `scope: ${scope}`;
}

// Pinning holds a box still while the layout is recomputed around it. The
// gesture is off: dragging read as clumsy and a better one is not designed
// yet. `pinned` is still honoured by the layout, so turning this back on
// is the whole change. Declared here because the node loop reads it.
const DRAG_TO_PIN = false;

// A claim is coloured by which worker holds it. Slots are assigned in
// first-seen order and stay with that identity for the whole timeline —
// colour follows the worker, never its rank in the current frame. Past
// six the pool cycles, so two sessions can share a hue; the session tag
// in the tooltip and the roster is what tells them apart, so identity
// never rests on colour alone. Validated for adjacent pairs in both
// modes, which is the case that arises: claims peak at a handful and sit
// scattered across the tree.
const POOL = ['w1', 'w2', 'w3', 'w4'];
const slotOf = new Map();
for (const e of D.events) {
  if (e.kind === 'task_claimed' && e.identity && !slotOf.has(e.identity))
    slotOf.set(e.identity, POOL[slotOf.size % POOL.length]);
}
// A holder with no slot is grey, never a hue: the amber this used to fall
// back to was the same value as slot four, so an unattributed holder was
// indistinguishable from a real worker.
const workerColour = id => `var(--${slotOf.get(id) || 'muted'})`;
const workerSoft = id => `var(--${slotOf.get(id) || 'muted'}-soft)`;
// Whichever of black or white the hue can actually carry. One ink per
// theme could not work: most of the fills failed their own label.
const workerInk = id =>
  slotOf.has(id) ? `var(--${slotOf.get(id)}-ink)` : 'var(--text-primary)';

// --- legend ---------------------------------------------------------------
// Flat, and one key stands for all workers; the roster below names them,
// folded, so a project with twenty sessions does not push the graph off
// the page.
function key(label, colour, shape) {
  const style = {
    fill:  `background:${colour};border:2px solid ${colour}`,
    ring:  `background:transparent;border:2px solid ${colour}`,
    soft:  `background:${colour};border:2px solid var(--w1)`,
    round: `background:transparent;border:2px solid ${colour};border-radius:9px`,
  }[shape];
  return `<span class="key"><span class="sw" style="${style}"></span>${label}</span>`;
}
document.getElementById('legend').innerHTML = [
  key('unpublished', 'var(--muted)', 'ring'),
  key('published', 'var(--ready)', 'ring'),
  key('claimed ▸ / held ○ — hue is the worker', 'var(--w1)', 'fill'),
  key(`${D.placeholder}-free body`, 'var(--w1-soft)', 'soft'),
  key(`${D.placeholder}-free with its deps`, 'var(--fill-clear)', 'fill'),
  key('theorem or lemma', 'var(--muted)', 'ring'),
  key('definition', 'var(--muted)', 'round'),
].join('');

const roster = document.getElementById('roster');
if (slotOf.size) {
  roster.innerHTML = `<summary>${slotOf.size} worker session`
    + (slotOf.size === 1 ? '' : 's')
    + ' — show which colour is whose</summary><ul>'
    + [...slotOf].map(([id, c]) =>
        `<li><span class="sw" style="background:${c};border:2px solid ${c}">`
        + `</span> ${id}</li>`).join('')
    + '</ul>';
} else {
  roster.remove();
}

document.getElementById('notes').innerHTML =
  D.notes.map(n => `<li>${n}</li>`).join('');

// --- build the scene once -------------------------------------------------
const nodesG = document.getElementById('nodes');
const edgesG = document.getElementById('edges');
const svg = document.getElementById('svg');
const byDecl = new Map(D.nodes.map(n => [n.decl, n]));
const el = new Map();
const links = [];
const neighbours = new Map();

for (const e of D.edges) {
  if (!byDecl.has(e.parent) || !byDecl.has(e.child)) continue;
  if (!neighbours.has(e.parent)) neighbours.set(e.parent, new Set());
  if (!neighbours.has(e.child)) neighbours.set(e.child, new Set());
  neighbours.get(e.parent).add(e.child);
  neighbours.get(e.child).add(e.parent);
  const p = document.createElementNS(NS, 'path');
  p.setAttribute('class', 'edge ' + e.source.replace('.', '-'));
  edgesG.appendChild(p);
  links.push({p, parent: e.parent, child: e.child, at: e.at || ''});
}

for (const n of D.nodes) {
  const g = document.createElementNS(NS, 'g');
  g.setAttribute('class', 'n');
  const label = n.decl.split('.').pop();
  const w = Math.max(58, Math.min(168, label.length * 7 + 16));
  const r = document.createElementNS(NS, 'rect');
  r.setAttribute('x', -w/2); r.setAttribute('y', -16);
  r.setAttribute('width', w); r.setAttribute('height', 32);
  // Shape carries the declaration kind, so a definition reads as one
  // without spending a colour channel on it.
  r.setAttribute('rx', n.kind === 'definition' ? 15 : 4);
  const t = document.createElementNS(NS, 'text');
  t.setAttribute('text-anchor', 'middle'); t.setAttribute('y', 4);
  t.textContent = label.length > 22 ? label.slice(0, 21) + '…' : label;
  const mark = document.createElementNS(NS, 'text');
  mark.setAttribute('class', 'mark');
  mark.setAttribute('text-anchor', 'middle');
  mark.setAttribute('x', w/2 + 8); mark.setAttribute('y', 5);
  const ring = document.createElementNS(NS, 'rect');
  ring.setAttribute('class', 'ring');
  ring.setAttribute('x', -w/2 - 6); ring.setAttribute('y', -22);
  ring.setAttribute('width', w + 12); ring.setAttribute('height', 44);
  ring.setAttribute('rx', 9);
  g.appendChild(r); g.appendChild(t); g.appendChild(mark); g.appendChild(ring);
  nodesG.appendChild(g);
  el.set(n.decl, {g, r, t, mark, w, x: 0, y: 0});
  g.addEventListener('mouseenter', () => lightUp(n.decl));
  g.addEventListener('mousemove', ev => showTip(ev, n));
  g.addEventListener('mouseleave', scheduleHide);
  if (DRAG_TO_PIN) {
    g.addEventListener('mousedown', ev => {
      ev.stopPropagation();          // do not start a canvas pan
      nodeDrag = {decl: n.decl, x: ev.clientX, y: ev.clientY};
    });
    g.addEventListener('dblclick', () => {
      pinned.delete(n.decl);
      el.get(n.decl).g.classList.remove('pinned');
      paint(+scrub.value);
    });
  }
}

let nodeDrag = null;

// --- layout, per frame ----------------------------------------------------
// Positions are recomputed from the subgraph that exists at this step, not
// from the finished one. A fixed layout would need the future, which rules
// out watching a project that is still running, and it draws early frames
// in coordinates that had not happened yet — two nodes sitting together
// early can end up far apart once the detail between them is filled in.
//
// Motion is kept followable rather than eliminated: within a layer nodes
// hold the order they first appeared in, so growth spreads outward instead
// of reshuffling, and each box eases to its new place via a CSS transition.
const X_STEP = 190, Y_STEP = 96, ROW_MAX = 26;
const firstSeen = new Map();
const pinned = new Map();

// Which end of the tree sits on top is a workflow question, not a
// convention one. Decomposition grows downward from the goal: publish C,
// a reduction splits it into B, B into A. Construction grows the other
// way: prove A, then B, then C. Both read best top-down, so the toggle
// flips the tree rather than only the arrowheads — and the head then
// always sits at the lower end, pointing the way the work flowed.
let goalFirst = true;

function layoutOf(visible) {
  const kids = new Map(), indeg = new Map();
  for (const d of visible) { kids.set(d, []); indeg.set(d, 0); }
  for (const l of links) {
    if (!visible.has(l.parent) || !visible.has(l.child)) continue;
    const from = goalFirst ? l.parent : l.child;
    const to = goalFirst ? l.child : l.parent;
    kids.get(from).push(to);
    indeg.set(to, indeg.get(to) + 1);
  }
  const layer = new Map([...visible].map(d => [d, 0]));
  const queue = [...visible].filter(d => indeg.get(d) === 0);
  const done = new Set();
  while (queue.length) {
    const d = queue.pop(); done.add(d);
    for (const k of kids.get(d)) {
      layer.set(k, Math.max(layer.get(k), layer.get(d) + 1));
      indeg.set(k, indeg.get(k) - 1);
      if (indeg.get(k) === 0) queue.push(k);
    }
  }
  const cyclic = [...visible].filter(d => !done.has(d));
  const rows = new Map();
  for (const d of visible) {
    if (!done.has(d)) continue;
    const L = layer.get(d);
    if (!rows.has(L)) rows.set(L, []);
    rows.get(L).push(d);
  }

  const pos = new Map();
  for (const [d, at] of pinned) if (visible.has(d)) pos.set(d, at);
  let y = 0;
  const place = (band, startY) => {
    band.sort((a, b) => (firstSeen.get(a) ?? 0) - (firstSeen.get(b) ?? 0));
    let lastY = startY;
    for (let i = 0; i < band.length; i += ROW_MAX) {
      const chunk = band.slice(i, i + ROW_MAX);
      const offset = -(chunk.length - 1) * X_STEP / 2;
      lastY = startY + (i / ROW_MAX) * (Y_STEP / 2);
      chunk.forEach((d, j) => {
        if (!pinned.has(d)) pos.set(d, {x: offset + j * X_STEP, y: lastY});
      });
    }
    return lastY;
  };
  for (const L of [...rows.keys()].sort((a, b) => a - b))
    y = place(rows.get(L), y) + Y_STEP;
  if (cyclic.length) place(cyclic, y + Y_STEP);
  return pos;
}

// --- replay ---------------------------------------------------------------
// Two channels, as a blueprint reads them: the border says what is known
// about the statement, the fill says what is known about the proof.
//
// A claim takes the whole box in its holder's colour, and the holder keeps
// the border until the node and everything under it is clear — so a
// worker that splits its task still visibly owns the split.
function stateAt(index) {
  // A `given` node is complete before the first commit and stays so: the
  // library provides it, or the prover named it and the scan has no name
  // to follow. Without this everything above one reads as unfinished.
  //
  // Complete is not the same as present, and `given` says nothing about
  // when. These are exactly the nodes no event mentions, so the fill is
  // seeded here and the placing is left to the neighbours, below.
  const given = D.nodes.filter(n => n.given).map(n => n.decl);
  const stated = new Set(), published = new Set(), claimed = new Set();
  const owner = new Map();
  const filled = new Set(given), retracted = new Set(), who = new Map();
  const removed = new Set();
  // An index into every event, not into the current scope's: the state of
  // a declaration the scope hides still decides what is drawn above it.
  const last = Math.min(index, D.events.length - 1);
  const cut = last >= 0 ? D.events[last].at : '';
  for (let i = 0; i <= last; i++) {
    const e = D.events[i];
    if (!e.decl) continue;
    if (e.actor) who.set(e.decl, e.identity || e.actor);
    switch (e.kind) {
      case 'node_stated':    stated.add(e.decl); removed.delete(e.decl); break;
      case 'task_published':
        // Re-publishing a declaration replaces its retraction: a node is
        // keyed by declaration, so this is the same node coming back.
        published.add(e.decl); stated.add(e.decl); retracted.delete(e.decl);
        break;
      case 'task_claimed':
        claimed.add(e.decl);
        owner.set(e.decl, e.identity || 'unattributed');
        break;
      case 'task_released':  claimed.delete(e.decl); owner.delete(e.decl); break;
      case 'pr_merged':      claimed.delete(e.decl); break;
      case 'pr_closed':
        // Closed unmerged is a rejection: the task is open again, so the
        // worker stops owning it. Only a merge leaves ownership behind.
        claimed.delete(e.decl); owner.delete(e.decl); break;
      case 'node_filled':
        filled.add(e.decl); stated.add(e.decl); removed.delete(e.decl); break;
      case 'node_removed':
        // Deleted from the source: the project no longer has it, so
        // nothing is known about either its statement or its body.
        removed.add(e.decl); stated.delete(e.decl); filled.delete(e.decl); break;
      case 'retracted':
        retracted.add(e.decl); claimed.delete(e.decl); owner.delete(e.decl);
        break;
    }
  }
  // Place the nodes no event mentions. Two bounds come off the edges and
  // both are honest: a declaration cannot precede what it uses, and it
  // must exist by the time something uses it. The earlier of the two is
  // the tightest the history supports, and either beats the start of the
  // history, which is what a node with no bound at all ends up claiming.
  //
  // The vacuous case is the one to keep out. A library result uses
  // nothing here, so "everything it uses is present" is true at the
  // first frame and would put it straight back where it began — hence
  // the bound counts only for a node that declares a dependency at all.
  // `derive` drops any library result nothing uses, so each one left has
  // a user to wait for.
  //
  // The fixpoint is for one of these standing on another: a single pass
  // would draw the upper one a frame late, and the edge between them
  // with it.
  if (given.length) {
    const users = new Map(), needs = new Map();
    for (const l of links) {
      if (l.at && l.at > cut) continue;
      if (!users.has(l.child)) users.set(l.child, []);
      users.get(l.child).push(l.parent);
      if (!needs.has(l.parent)) needs.set(l.parent, []);
      needs.get(l.parent).push(l.child);
    }
    const drawn = d => stated.has(d) || published.has(d) || retracted.has(d);
    for (let again = true; again; ) {
      again = false;
      for (const d of given) {
        if (stated.has(d)) continue;
        const deps = needs.get(d) || [];
        if (!(users.get(d) || []).some(drawn)
            && !(deps.length && deps.every(drawn))) continue;
        stated.add(d);
        again = true;
      }
    }
  }

  // Transitive over declared edges only: the body carries no placeholder
  // and neither does any dependency the roadmap names. An unrecorded edge
  // is invisible here, so this is a reading of the plan, not of the kernel.
  const kids = new Map();
  for (const l of links) {
    if (l.at && l.at > cut) continue;
    if (!kids.has(l.parent)) kids.set(l.parent, []);
    kids.get(l.parent).push(l.child);
  }
  const memo = new Map();
  const clear = d => {
    if (memo.has(d)) return memo.get(d);
    memo.set(d, false);
    if (!filled.has(d)) return false;
    const ok = (kids.get(d) || []).every(clear);
    memo.set(d, ok); return ok;
  };
  const fully = new Set([...filled].filter(clear));
  // The holder's colour is released once the node and everything under it
  // are clear — but never while the claim is still live, or a node would
  // go unowned while its own pull request is still open.
  for (const d of fully) if (!claimed.has(d)) owner.delete(d);
  return {stated, published, claimed, owner, filled, fully, retracted, removed,
          who, cut};
}

/** Point the head down the page, and at the size the edge is drawn. */
function applyHead(l) {
  const m = l.p.classList.contains('lit') ? 'url(#arLit)' : 'url(#ar)';
  l.p.setAttribute('marker-start', l.head === 'start' ? m : 'none');
  l.p.setAttribute('marker-end', l.head === 'end' ? m : 'none');
}

/** Distance from a box's centre to where a line leaving it clears the box. */
function clearance(E, ux, uy) {
  const hw = E.w/2 + 5, hh = 21;
  const tx = Math.abs(ux) > 1e-4 ? hw / Math.abs(ux) : Infinity;
  const ty = Math.abs(uy) > 1e-4 ? hh / Math.abs(uy) : Infinity;
  return Math.min(tx, ty);
}

function paint(index) {
  const cut = active.length ? active[Math.min(index, active.length - 1)] : -1;
  const S = stateAt(cut);
  const visible = new Set();
  for (const n of D.nodes)
    if ((S.stated.has(n.decl) || S.published.has(n.decl)
         || S.retracted.has(n.decl)) && inScope(n.decl)) {
      visible.add(n.decl);
      if (!firstSeen.has(n.decl)) firstSeen.set(n.decl, cut);
    }

  const pos = layoutOf(visible);
  let minX = 1e9, maxX = -1e9, minY = 1e9, maxY = -1e9;

  for (const n of D.nodes) {
    const E = el.get(n.decl);
    if (!visible.has(n.decl)) { E.g.classList.add('hidden'); continue; }
    E.g.classList.remove('hidden');
    const p = pos.get(n.decl) || {x: 0, y: 0};
    E.x = p.x; E.y = p.y;
    E.g.setAttribute('transform', `translate(${p.x},${p.y})`);
    minX = Math.min(minX, p.x - E.w/2); maxX = Math.max(maxX, p.x + E.w/2);
    minY = Math.min(minY, p.y - 16);    maxY = Math.max(maxY, p.y + 16);

    const own = S.owner.get(n.decl);
    // border: what is known about the statement
    let stroke = 'var(--muted)';
    if (S.retracted.has(n.decl))      stroke = 'var(--critical)';
    else if (own)                     stroke = workerColour(own);
    else if (S.fully.has(n.decl))     stroke = 'var(--fill-clear)';
    else if (S.published.has(n.decl)) stroke = 'var(--ready)';

    // fill: how far the proof has got. Depth of fill is the progress and
    // hue is the owner, so a node half-done in a worker's hands reads the
    // same way as one half-done in nobody's. An owner that is no longer
    // claiming means a merge landed, so such a node is never left empty.
    let fill = 'transparent', ink = 'var(--text-primary)';
    if (S.claimed.has(n.decl)) {
      fill = workerColour(own); ink = workerInk(own);
    } else if (S.fully.has(n.decl)) {
      fill = 'var(--fill-clear)'; ink = 'var(--on-clear)';
    } else if (S.filled.has(n.decl) || own) {
      fill = own ? workerSoft(own) : 'var(--ready-soft)';
    }

    E.r.setAttribute('stroke', stroke);
    E.r.setAttribute('fill', fill);
    E.t.setAttribute('fill', ink);
    // Status never rides on colour alone.
    E.mark.textContent = S.retracted.has(n.decl) ? '✕'
                       : S.claimed.has(n.decl)   ? '▸'
                       : own                     ? '○' : '';
    E.mark.setAttribute('fill', S.retracted.has(n.decl) ? 'var(--critical)'
                              : own ? workerColour(own) : 'var(--muted)');
  }

  for (const l of links) {
    const a = el.get(l.parent), b = el.get(l.child);
    const bothDrawn = visible.has(l.parent) && visible.has(l.child);
    const dueYet = !l.at || l.at <= S.cut;
    l.p.style.visibility = (bothDrawn && dueYet) ? 'visible' : 'hidden';
    if (!bothDrawn) continue;
    // The line stops where it clears each box, so the head grows out of
    // the line instead of landing on top of the node.
    const dy = b.y - a.y;
    if (Math.abs(dy) >= 60) {
      // A curve that leaves the bottom of one box and enters the top of
      // the next, the way a blueprint's graph draws it. It is also the
      // more honest route: a straight diagonal between distant columns
      // exits through a box's side and crosses whatever shares its row,
      // while this does its travelling in the gap between rows. Vertical
      // tangents at both ends keep the head square to the box it meets.
      const s = dy > 0 ? 1 : -1;
      const y1 = a.y + s * 22, y2 = b.y - s * 22;
      const m = ((y1 + y2) / 2).toFixed(1);
      l.p.setAttribute('d',
        `M${a.x},${y1.toFixed(1)} C${a.x},${m} ${b.x},${m} ${b.x},${y2.toFixed(1)}`);
      l.head = s > 0 ? 'end' : 'start';
    } else {
      // Same row, which the cycle band produces: no gap to curve through,
      // so a straight line trimmed at each box.
      const dx = b.x - a.x;
      const d = Math.hypot(dx, dy) || 1;
      const ux = dx/d, uy = dy/d;
      const from = clearance(a, ux, uy), to = clearance(b, ux, uy);
      if (from + to + 3 > d) { l.p.style.visibility = 'hidden'; continue; }
      l.p.setAttribute('d',
        `M${(a.x + ux*from).toFixed(1)},${(a.y + uy*from).toFixed(1)} `
        + `L${(b.x - ux*to).toFixed(1)},${(b.y - uy*to).toFixed(1)}`);
      l.head = 'end';
    }
    applyHead(l);
  }

  if (follow && visible.size) fit(minX, maxX, minY, maxY);
  // Name the event: not every step moves a box, and a step that does not
  // should say what it was rather than look like nothing happened.
  const e = cut >= 0 ? D.events[cut] : null;
  document.getElementById('when').textContent = e
    ? `${e.at.slice(0,16).replace('T',' ')}  ·  ${index+1}/${active.length}`
      + `  ·  ${e.kind.replace(/_/g, ' ')}`
      + (e.decl ? ' ' + e.decl.split('.').pop() : '') : 'no events';
}

// --- highlight and tooltip ------------------------------------------------
// The tooltip is hoverable: the pointer can move into it to select text,
// and it closes shortly after leaving both it and the node.
const tip = document.getElementById('tip');
let hideTimer = null;
tip.addEventListener('mouseenter', () => clearTimeout(hideTimer));
tip.addEventListener('mouseleave', scheduleHide);

function scheduleHide() {
  clearTimeout(hideTimer);
  hideTimer = setTimeout(() => { tip.style.display = 'none'; clearLight(); }, 280);
}

let highlight = true;
document.getElementById('hl').addEventListener('click', ev => {
  highlight = !highlight;
  ev.currentTarget.textContent = 'highlight: ' + (highlight ? 'on' : 'off');
  if (!highlight) svg.classList.remove('focusing');
});

function lightUp(decl) {
  clearTimeout(hideTimer);
  // Edges always. They are thin and the tree is dense later on, so
  // picking one out is the main reason to hover; the toggle only decides
  // whether everything else dims around it.
  for (const l of links) {
    l.p.classList.toggle('lit', l.parent === decl || l.child === decl);
    applyHead(l);
  }
  if (!highlight) return;
  svg.classList.add('focusing');
  const lit = new Set([decl, ...(neighbours.get(decl) || [])]);
  for (const [d, E] of el) E.g.classList.toggle('lit', lit.has(d));
}

function clearLight() {
  svg.classList.remove('focusing');
  for (const [, E] of el) E.g.classList.remove('lit');
  for (const l of links) { l.p.classList.remove('lit'); applyHead(l); }
}

function showTip(ev, n) {
  if (nodeDrag) return;
  clearTimeout(hideTimer);
  const S = stateAt(active.length
    ? active[Math.min(+scrub.value, active.length - 1)] : -1);
  const own = S.owner.get(n.decl);
  const statement = S.removed.has(n.decl)   ? 'removed from the source'
                  : S.retracted.has(n.decl) ? 'retracted ✕'
                  : S.claimed.has(n.decl)   ? `claimed ▸ by ${own || '?'}`
                  : own                     ? `still ${own}\\u2019s — merged, not closed`
                  : S.published.has(n.decl) ? 'published — open for work'
                  : 'in the plan, not published';
  const proof = S.removed.has(n.decl)
        ? 'the project does not have this declaration'
      : S.fully.has(n.decl)
        ? `${D.placeholder}-free, and so is every dependency the roadmap declares`
      : S.filled.has(n.decl)
        ? `${D.placeholder}-free, but a declared dependency is not`
        : `still carries a ${D.placeholder}`;
  const near = (neighbours.get(n.decl) || new Set()).size;
  const rows = [
    ['statement', statement],
    ['proof', proof],
    ['kind', n.kind || 'theorem'],
    ['last touched by', S.who.get(n.decl) || '—'],
    ['file', n.file || '—'],
    ['group', n.group || '—'],
    ['task', n.issue ? '#' + n.issue : '—'],
    ['declared edges', near ? String(near) : 'none — drawn unattached'],
  ];
  tip.innerHTML = `<b>${n.decl}</b><dl>`
    + rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('') + '</dl>';
  tip.style.display = 'block';
  const box = tip.getBoundingClientRect();
  tip.style.left = Math.max(4, Math.min(ev.clientX + 14,
    innerWidth - box.width - 8)) + 'px';
  tip.style.top = Math.max(4, Math.min(ev.clientY + 14,
    innerHeight - box.height - 8)) + 'px';
}

// --- table view (identity never colour alone) -----------------------------
const tbody = document.querySelector('#eventTable tbody');
const sortTri = document.getElementById('sortTri');
let cued = null, cueCentre = null, newestFirst = true;

// The head of a growing log is the part being read, so it comes first. The
// order is the array's own, reversed over a copy: `active` indexes
// `D.events` by position, and two events in the same minute print the same
// timestamp but are not interchangeable.
function renderRows() {
  uncue();
  sortTri.innerHTML = newestFirst ? '&#9662;' : '&#9652;';
  const rows = newestFirst ? D.events.slice().reverse() : D.events;
  tbody.innerHTML = rows.map(e => `<tr>
  <td>${e.at.slice(0,16).replace('T',' ')}</td><td>${e.kind}</td>
  <td class="decl" data-decl="${e.decl || ''}">${e.decl || ''}</td>
  <td>${e.identity || e.actor}</td>
  <td>${e.pr ? 'PR #' + e.pr : (e.issue ? '#' + e.issue : '')}</td></tr>`).join('');
}
renderRows();
document.getElementById('sortWhen').addEventListener('click', () => {
  newestFirst = !newestFirst;
  renderRows();
});

// Hovering a name here lights its node and edges exactly as hovering the
// box does, and brings the box to the middle of the tree — a name in a
// table of hundreds is no use if the box it belongs to is off the side of
// the camera. One delegated listener rather than one per row, so the rows
// can be rebuilt in either order beneath it, and the cell carries the full
// name the label on the box is abbreviated from.
function cue(cell) {
  if (!cell || !cell.dataset.decl) cell = null;
  if (cued === cell) return;
  uncue();
  if (!cell) return;
  cued = cell;
  const E = el.get(cell.dataset.decl);
  // A row the scrubber has not reached has no box yet. Moving time to it
  // is the find box's job, asked for; a hover that jumped the timeline
  // would move the tree out from under the reader.
  if (!E || E.g.classList.contains('hidden')) { cell.classList.add('absent'); return; }
  // `lightUp` holds the tooltip open, which is right when the pointer is
  // on a box and wrong when it has left for the table.
  tip.style.display = 'none';
  E.g.classList.add('cued');
  lightUp(cell.dataset.decl);
  // A pointer sweeping down the table crosses every row on the way. The
  // pause is what separates reading a row from passing over it.
  cueCentre = setTimeout(() => { handOver(); centreNode(E); }, 120);
}

function uncue() {
  clearTimeout(cueCentre);
  if (!cued) return;
  cued.classList.remove('absent');
  const E = el.get(cued.dataset.decl);
  if (E) E.g.classList.remove('cued');
  cued = null;
  clearLight();
}

tbody.addEventListener('mouseover', ev => cue(ev.target.closest('td.decl')));
tbody.addEventListener('mouseleave', uncue);

// --- controls -------------------------------------------------------------
const scrub = document.getElementById('scrub');
rebuildScope();
scrub.value = scrub.max;
scrub.addEventListener('input', () => { stepBefore = null; paint(+scrub.value); });

// Switching scope changes what the scrubber indexes, so hold the moment
// rather than the number: the same instant in the other scope is the
// nearest event at or before it.
document.getElementById('scope').addEventListener('click', () => {
  const at = active.length
    ? D.events[active[Math.min(+scrub.value, active.length - 1)]].at : '';
  scope = scope === 'plan' ? 'all' : 'plan';
  rebuildScope();
  let i = active.length - 1;
  while (i > 0 && D.events[active[i]].at > at) i--;
  scrub.value = i;
  paint(i);
  // The other scope holds a different set of nodes and indexes the
  // scrubber differently, so a live search is answered again from scratch.
  if (find.value.trim()) { stepBefore = null; runFind(); }
});

// Step delay, slowest to fastest. A bulk push can put eighty events in
// one second of project time, so the slow end matters: at 400ms a batch
// unfolds one declaration at a time instead of flashing past.
const DELAYS = [800, 400, 200, 90, 40, 16];
const speed = document.getElementById('speed');
const speedLabel = document.getElementById('speedLabel');
const play = document.getElementById('play');
let timer = null;

const delay = () => DELAYS[+speed.value];
function showSpeed() { speedLabel.textContent = delay() + ' ms/step'; }
showSpeed();
speed.addEventListener('input', () => {
  showSpeed();
  if (timer) { stop(); start(); }   // take effect without losing the position
});

function stop() { clearInterval(timer); timer = null; play.textContent = '▶ Play'; }
function start() {
  play.textContent = '❚❚ Pause';
  timer = setInterval(() => {
    if (+scrub.value >= +scrub.max) { stop(); return; }
    stepBefore = null;
    scrub.value = +scrub.value + 1;
    paint(+scrub.value);
  }, delay());
}
play.addEventListener('click', () => {
  if (timer) { stop(); return; }
  if (+scrub.value >= +scrub.max) scrub.value = 0;
  start();
});
document.getElementById('toggleTable').addEventListener('click', () => {
  const v = document.getElementById('tableView');
  const open = v.style.display !== 'block';
  v.style.display = open ? 'block' : 'none';
  document.body.classList.toggle('tabling', open);
  // The plot changes height with the class, and the camera is fitted to a
  // height, so the tree is laid out again rather than left cropped.
  paint(+scrub.value);
});
document.getElementById('toggleTheme').addEventListener('click', () => {
  const r = document.documentElement;
  r.dataset.theme = r.dataset.theme === 'dark' ? 'light' : 'dark';
});

// --- camera ---------------------------------------------------------------
// The camera fits the visible subgraph, so a handful of early nodes fill
// the view and it pulls back as the tree grows. Touching pan or zoom hands
// control over; Fit gives it back.
const cam = document.getElementById('camera');
let zoom = 0.85, panX = 0, panY = 40, canvasDrag = null, follow = true;
document.getElementById('flow').addEventListener('click', ev => {
  goalFirst = !goalFirst;
  ev.currentTarget.textContent = goalFirst
    ? 'flow: goal → obligations' : 'flow: foundations → results';
  paint(+scrub.value);
});

function applyCamera() {
  const b = svg.getBoundingClientRect();
  cam.setAttribute('transform',
    `translate(${b.width/2 + panX},${panY}) scale(${zoom})`);
}

function fit(minX, maxX, minY, maxY) {
  const b = svg.getBoundingClientRect();
  const pad = 48;
  const w = Math.max(1, maxX - minX), h = Math.max(1, maxY - minY);
  zoom = Math.max(0.12, Math.min(1.6,
    Math.min((b.width - pad*2) / w, (b.height - pad*2) / h)));
  panX = -((minX + maxX) / 2) * zoom;
  panY = pad - minY * zoom;
  applyCamera();
}

function handOver() {
  if (!follow) return;
  follow = false;
  document.getElementById('fit').textContent = 'Fit ▸ manual';
}

// A trackpad pinch reaches the page as a wheel event, so both gestures
// come through here. The point under the pointer is the one held fixed:
// zooming the canvas's top centre instead means the thing being looked at
// slides off the screen just as it gets big enough to read.
let zoomSettle = null;
svg.addEventListener('wheel', ev => {
  ev.preventDefault(); followBefore = null; handOver();
  const b = svg.getBoundingClientRect();
  // `deltaY` is only a number until `deltaMode` says what of. Gecko counts
  // a pinch in lines and reports ±1; Blink and WebKit count the same
  // gesture in pixels and report tens or hundreds. Read as pixels
  // throughout, a Firefox pinch would ask for a one-percent step.
  const px = ev.deltaY * (ev.deltaMode === 1 ? 16
                        : ev.deltaMode === 2 ? b.height : 1);
  // A pinch emits a stream of small deltas, so the step follows the
  // delta's size; one line, or a wheel's single large delta, is clamped to
  // the step the wheel always had.
  const next = Math.max(0.08, Math.min(2.4,
    zoom * Math.exp(Math.max(-0.1, Math.min(0.1, -px * 0.01)))));
  const cx = ev.clientX - b.left, cy = ev.clientY - b.top;
  // The camera places a graph point at `width/2 + panX + zoom*x`. Solve
  // that for the pan which leaves the cursor's own point where it is.
  panX = cx - b.width/2 - (cx - b.width/2 - panX) * next / zoom;
  panY = cy - (cy - panY) * next / zoom;
  zoom = next;
  cam.classList.add('nudging');
  clearTimeout(zoomSettle);
  zoomSettle = setTimeout(() => cam.classList.remove('nudging'), 200);
  applyCamera();
}, {passive: false});
svg.addEventListener('mousedown', ev => {
  canvasDrag = {x: ev.clientX, y: ev.clientY};
});
addEventListener('mouseup', () => { canvasDrag = null; nodeDrag = null; });
addEventListener('mousemove', ev => {
  if (nodeDrag) {
    const E = el.get(nodeDrag.decl);
    pinned.set(nodeDrag.decl, {
      x: E.x + (ev.clientX - nodeDrag.x) / zoom,
      y: E.y + (ev.clientY - nodeDrag.y) / zoom,
    });
    E.g.classList.add('pinned');
    nodeDrag = {decl: nodeDrag.decl, x: ev.clientX, y: ev.clientY};
    paint(+scrub.value);
    return;
  }
  if (!canvasDrag) return;
  followBefore = null; handOver();
  panX += ev.clientX - canvasDrag.x; panY += ev.clientY - canvasDrag.y;
  canvasDrag = {x: ev.clientX, y: ev.clientY};
  applyCamera();
});
addEventListener('resize', () => paint(+scrub.value));
document.getElementById('fit').addEventListener('click', ev => {
  followBefore = null;
  follow = true; ev.currentTarget.textContent = 'Fit ▸ auto';
  paint(+scrub.value);
});

// --- find a declaration ---------------------------------------------------
// The browser's own find cannot do this job, for three reasons at once: a
// label is abbreviated to the last namespace component and clipped at 22
// characters, so the name searched for is not the text on the box; a node
// the scrubber has not reached is `display: none`, which find-in-page skips;
// and the graph is a camera over an SVG rather than a scrolling page, so
// even a match the browser did find could not be brought into view. Hence
// the page carries its own.
const find = document.getElementById('find');
const findCount = document.getElementById('findCount');
let matches = [], hit = -1, stepBefore = null, followBefore = null;

// Visibility only ever accumulates — `stated`, `published` and `retracted`
// are added to, and a retraction that is replaced is re-published in the
// same step — so the first event naming a declaration in one of these kinds
// is the step at which its box appears.
function firstStep(decl) {
  for (let i = 0; i < D.events.length; i++) {
    const e = D.events[i];
    if (e.decl !== decl) continue;
    if (e.kind !== 'node_stated' && e.kind !== 'task_published'
        && e.kind !== 'node_filled' && e.kind !== 'retracted') continue;
    for (let j = 0; j < active.length; j++) if (active[j] >= i) return j;
    return active.length - 1;
  }
  return -1;
}

function applyFind() {
  svg.classList.toggle('finding', matches.length > 0);
  const found = new Set(matches);
  for (const [d, E] of el) {
    E.g.classList.toggle('found', found.has(d));
    E.g.classList.toggle('current', hit >= 0 && d === matches[hit]);
  }
}

function centreNode(E) {
  const b = svg.getBoundingClientRect();
  panX = -E.x * zoom;
  panY = b.height / 2 - E.y * zoom;
  applyCamera();
}

function centreOn(decl) {
  const E = el.get(decl);
  if (!E || E.g.classList.contains('hidden')) return;
  // Borrowed, not taken: auto-fit would pull straight back out, but the
  // reader did not ask for manual — clearing the search gives it back.
  if (followBefore === null) followBefore = follow;
  handOver();
  centreNode(E);
}

function goTo(i) {
  if (!matches.length) return;
  hit = (i + matches.length) % matches.length;
  const decl = matches[hit];
  // A match the scrubber has not reached yet is the original complaint in
  // another form, so move time to it rather than report nothing found.
  const E = el.get(decl);
  if (E && E.g.classList.contains('hidden')) {
    const step = firstStep(decl);
    if (step >= 0) {
      if (stepBefore === null) stepBefore = +scrub.value;
      scrub.value = String(step);
      paint(step);
    }
  }
  findCount.textContent = (hit + 1) + '/' + matches.length;
  applyFind();
  centreOn(decl);
}

function runFind() {
  const q = find.value.trim().toLowerCase();
  hit = -1;
  matches = q
    ? D.nodes.filter(n => inScope(n.decl) && n.decl.toLowerCase().includes(q))
             .map(n => n.decl)
    : [];
  if (!q) {
    // Put back whatever the search borrowed and the reader has not since
    // claimed: the moment it moved time from, and auto-fit.
    const restore = stepBefore !== null || followBefore === true;
    if (stepBefore !== null) scrub.value = String(stepBefore);
    if (followBefore === true) {
      follow = true;
      document.getElementById('fit').textContent = 'Fit ▸ auto';
    }
    stepBefore = null; followBefore = null;
    findCount.textContent = '';
    applyFind();
    if (restore) paint(+scrub.value);
    return;
  }
  if (!matches.length) { findCount.textContent = 'no match'; applyFind(); return; }
  goTo(0);
}

find.value = '';               // a reload can hand back the previous value
find.addEventListener('input', runFind);
find.addEventListener('keydown', ev => {
  if (ev.key === 'Enter') { ev.preventDefault(); goTo(hit + (ev.shiftKey ? -1 : 1)); }
  else if (ev.key === 'Escape') { find.value = ''; runFind(); }
});
document.getElementById('findNext').addEventListener('click', () => goTo(hit + 1));
document.getElementById('findPrev').addEventListener('click', () => goTo(hit - 1));
addEventListener('keydown', ev => {
  // Ctrl/Cmd-F is the key a reader actually reaches for, and the panel it
  // opens cannot search the graph. Take it there — but the table is real
  // text, carrying actors and PR numbers this box does not index, so while
  // it is open the browser's own find is the better one.
  if ((ev.metaKey || ev.ctrlKey) && ev.key === 'f') {
    if (document.getElementById('tableView').style.display === 'block') return;
    ev.preventDefault(); find.focus(); find.select();
  } else if (ev.key === '/' && document.activeElement !== find) {
    ev.preventDefault(); find.focus();
  }
});

paint(+scrub.value);
</script>
</body>
</html>
"""


def in_plan(node: Node) -> bool:
    """Whether the plan names this declaration.

    Two things put a node here, and a person decides both: a published
    task, and a place in a roadmap group. Everything else is a
    declaration someone wrote while proving one of those — helper
    lemmas, which belong in the graph and carry edges like any other
    declaration, but are not what the project set out to prove. That is
    the difference between the two scopes, and why `all` is the view to
    read when asking what the work actually took.

    A worker's decomposition stays here without anyone filing it: the
    children are published as tasks, and a task is the project saying it
    means to prove the thing. Declaring a dependency is not such a
    decision — once a plan records the edges its terms really have,
    almost every declaration has one, and counting that would put the
    corpus in both views and leave neither answering its own question.

    Being outside the plan costs a declaration nothing but this filter.
    Edges are drawn from every node, and a proof's completeness is
    computed over every declared edge regardless of scope, so a helper
    still decides whether the theorem above it reads as clear.
    """
    return bool(node.issue or node.group)


def _scene(timeline: Timeline, *, scope: str) -> dict[str, object]:
    """Nodes, edges and events as the page consumes them.

    Everything is emitted, each node flagged with whether the plan names
    it, and `scope` sets which the page shows first. Filtering here
    instead would need a second file to see the other view, and the two
    answer different questions — the plan is the tree the project
    intends, and the whole corpus shows how much scaffolding it took.
    """
    nodes = dict(timeline.nodes)
    edges = list(timeline.edges)
    events = list(timeline.events)

    layout = place(nodes, edges)
    depth = {p.decl: p.layer for p in layout.placed}

    # Commit timestamps are second-granularity, so a bulk push lands many
    # commits in one second. Sequence orders those by the order they were
    # made, then the lifecycle order settles two events of one act — a
    # proof reads before the merge that carried it — and depth orders the
    # declarations within one commit so a batch reads root-first rather
    # than in dictionary order.
    def _order(event: Event) -> tuple[str, int, int, int, str]:
        return (
            event.at,
            event.seq,
            KIND_RANK[event.kind],
            depth.get(event.decl, 999),
            event.decl,
        )

    events = sorted(events, key=_order)
    profile = PROFILES.get(timeline.prover, PROFILES["lean4"])
    word = profile.placeholder_tokens[0]
    return {
        # The prover's own word for an unfilled body, so the page reads
        # right on a Rocq project as well as a Lean one.
        "placeholder": word,
        "scope": scope,
        "nodes": [
            {**asdict(node), "plan": in_plan(node)} for node in nodes.values()
        ],
        "edges": [asdict(e) for e in edges],
        "events": [
            {
                "at": e.at,
                "kind": e.kind.value,
                "actor": e.actor.value,
                "decl": e.decl,
                "issue": e.issue,
                "pr": e.pr,
                "identity": e.identity.label if e.identity else "",
                "detail": e.detail,
                "seq": e.seq,
            }
            for e in events
        ],
        "notes": [
            "The layout is recomputed from whatever exists at each step, "
            "so the tree grows rather than filling in a finished shape. A "
            "node moves when something it depends on, or something that "
            "depends on it, appears above or below it \u2014 usually a "
            "dependency being declared, or a reduction naming it as an "
            "obligation.",
            "Flow picks which end of the tree sits on top, because that "
            "depends on how the work ran. Goal-first reads a "
            "decomposition downward: the goal, then the obligations a "
            "reduction split it into. Foundations-first reads a "
            "construction downward: what was filled first, then what it "
            "made provable. Arrows point down the page either way.",
            "A thin edge is a dependency the plan declared. A thick one "
            "was declared by a worker's reduction \u2014 an obligation it "
            "named when it split the task above.",
            f"A node counts as {word}-free with its deps when nothing the "
            f"roadmap declares below it carries the token. A dependency "
            f"the roadmap never recorded is invisible here.",
        ]
        + list(timeline.notes)
        + (
            [
                f"{len(layout.cycle)} nodes sit in a cycle of declared "
                "edges: " + ", ".join(layout.cycle[:6])
                + ("…" if len(layout.cycle) > 6 else "")
            ]
            if layout.cycle
            else []
        ),
    }


def render(timeline: Timeline, *, scope: str = "plan") -> str:
    """One HTML document, self-contained."""
    scene = _scene(timeline, scope=scope)
    filled = sum(
        1 for e in timeline.events if e.kind is EventKind.NODE_FILLED
    )
    subtitle = f"{filled} placeholders filled · prover {timeline.prover}"
    return (
        _TEMPLATE.replace("__TITLE__", html.escape(timeline.repo))
        .replace("__SUBTITLE__", html.escape(subtitle))
        .replace("__PLACEHOLDER__", html.escape(str(scene["placeholder"])))
        .replace("__DATA__", json.dumps(scene, separators=(",", ":")))
    )


def summary(timeline: Timeline, *, scope: str = "plan") -> dict[str, int]:
    """How much `render` will draw — the numbers worth logging.

    The timeline holds a project's whole corpus; the page shows what the
    scope keeps. Reporting the former as if it were the latter overstated
    the picture by a factor of three.
    """
    scene = _scene(timeline, scope=scope)
    nodes = [n for n in scene["nodes"] if scope == "all" or n["plan"]]
    shown = {n["decl"] for n in nodes}
    edges = [
        e for e in scene["edges"]
        if e["parent"] in shown and e["child"] in shown
    ]
    events = [
        e for e in scene["events"] if not e["decl"] or e["decl"] in shown
    ]
    return {"nodes": len(nodes), "edges": len(edges), "events": len(events)}


__all__ = ["render", "summary"]
