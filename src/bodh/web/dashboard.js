/* bodh: the live layer. The server rendered every panel from the document; this adds what needs
   a running page: the filters, the sortable headers, the tooltips, the verdict menus, the levers
   with their confirmations, and in the console the ticks that keep the state line, the meter and
   the tiles moving, with a refetch that swaps nodes in place when the engine says the page changed.
   Plain ES2020, no build step, no dependency; DOM writes are textContent or built nodes. */
(function () {
  'use strict';
  const root = document.documentElement;
  const mode = root.dataset.mode || 'export';
  const live = mode !== 'export'; // the export is a snapshot: filters and tooltips only
  root.classList.add('js');
  const $ = (sel, from) => (from || document).querySelector(sel);
  const $$ = (sel, from) => Array.from((from || document).querySelectorAll(sel));
  const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const HEADERS = { 'Content-Type': 'application/json', 'X-Bodh': 'console' };
  const KEYS = { 1: 'great', 2: 'good', 3: 'meh', 4: 'bad', 5: 'basic', 6: 'heavy', 7: 'more', 8: 'less', r: 'rescue' };
  const warn = (code, detail) => console.warn('bodh:' + code, detail === undefined ? '' : detail);

  // -- words and small DOM helpers ----------------------------------------------------------
  function ago(ms) {
    const s = Math.max(0, Math.round(ms / 1000)), m = Math.round(s / 60), h = Math.round(m / 60);
    return s < 60 ? s + ' s ago' : m < 60 ? m + ' min ago' : h < 48 ? h + ' h ago' : Math.round(h / 24) + ' days ago';
  }
  const count = (n) => Number(n).toLocaleString('en-GB');
  function wash(el) {
    if (!el || reduced) return;
    el.classList.remove('changed'); void el.offsetWidth; el.classList.add('changed'); // restarts on a second change
    el.addEventListener('animationend', () => el.classList.remove('changed'), { once: true });
  }
  function setText(el, text) { if (el && el.textContent !== text) { el.textContent = text; wash(el); } }
  function button(label, kind, attrs) {
    const b = document.createElement('button');
    b.type = 'button'; b.className = ('button ' + (kind || '')).trim(); b.textContent = label;
    Object.entries(attrs || {}).forEach(([k, v]) => b.setAttribute(k, v));
    return b;
  }
  function say(text, failed) {
    const n = $('#notice');
    if (!n) return;
    n.textContent = text; n.classList.toggle('failed', !!failed); clearTimeout(say.timer);
    if (text && !failed) say.timer = setTimeout(() => { if (n.textContent === text) n.textContent = ''; }, 12000);
  }
  function note(text) { const n = $('#live-note'); if (n) n.textContent = text; }
  // -- freshness: the stamp ticks; two missed cycles turn it to the warning colour -----------
  const stamp = $('#stamp');
  let asOf = stamp ? Date.parse(stamp.dataset.asOf) : NaN;
  let lastContact = Date.now();
  let cycle = 15000; // the server's heartbeat; 30 s when polling
  let stale = false;
  let sentence = $('#state-line') ? $('#state-line').textContent : '';
  function clock() {
    if (stamp && !isNaN(asOf)) { stamp.textContent = 'updated ' + ago(Date.now() - asOf); stamp.title = new Date(asOf).toLocaleString(); }
    const missed = live && Date.now() - lastContact > 2 * cycle;
    if (missed === stale) return;
    stale = missed;
    if (stamp) stamp.classList.toggle('stale', stale);
    const line = $('#state-line');
    if (line) line.textContent = stale ? 'The console is not answering; the numbers are from ' + ago(Date.now() - asOf) + '.' : sentence;
  }
  setInterval(clock, 1000); clock();

  // -- the ticks: state and tiles in place, a refetch when the generation moves ---------------
  const meta = $('meta[name="bodh-generation"]');
  let generation = meta ? meta.content : null;
  let lastChild = null; // the console's own command, re-applied after a refetch (the document knows nothing of it)
  function onTick(t) {
    lastContact = Date.now();
    if (t.as_of) asOf = Date.parse(t.as_of);
    if (stale) clock();
    if (t.state) applyState(t.state);
    if (t.today) applyToday(t.today);
    lastChild = t.child || null;
    applyChild(lastChild);
    const g = String(t.generation);
    if (generation !== null && g !== generation) refetch();
    generation = g;
  }
  function applyState(st) {
    sentence = st.sentence;
    if (!stale) setText($('#state-line'), sentence);
    setText($('.state .chip'), st.mode_label);
    const holder = $('#state-meter');
    if (holder && !(st.running && st.progress_total)) holder.replaceChildren();
    else if (holder) {
      let m = $('.meter', holder);
      if (!m) {
        m = document.createElement('div'); m.className = 'meter'; m.setAttribute('role', 'meter'); m.setAttribute('aria-valuemin', '0');
        m.append(document.createElement('span')); holder.replaceChildren(m);
      }
      m.setAttribute('aria-valuemax', st.progress_total); m.setAttribute('aria-valuenow', st.progress_done || 0); m.setAttribute('aria-label', st.phase || 'progress');
      m.firstElementChild.style.width = Math.max(0, Math.min(100, 100 * (st.progress_done || 0) / st.progress_total)).toFixed(1) + '%';
    }
    $$('.levers [data-action]').forEach((b) => { b.hidden = !(st.levers || []).includes(b.dataset.action === 'schedule_pause' ? 'pause' : b.dataset.action); });
  }
  function applyToday(t) {
    setText($('#hero-unread'), count(t.unread));
    (t.tiles || []).forEach((tile) => {
      const el = $('.tile[data-tile="' + tile.key + '"]');
      let d = el && $('.delta', el);
      if (!el) return;
      setText($('.value', el), tile.text);
      if (!d && tile.delta_text) { d = document.createElement('span'); $('.value', el).after(d); }
      if (d) { d.className = ('delta ' + (tile.direction || '')).trim(); setText(d, tile.delta_text); }
    });
  }
  function applyChild(c) {
    const out = $('#output');
    if (!out || !c || !c.args || !c.args.length) return;
    const name = 'bodh ' + c.args.join(' ');
    const text = name + (c.running ? ' · running' : ' · exit ' + c.code) + '\n' + (c.output || []).join('\n');
    if (out.textContent !== text) { out.textContent = text; out.hidden = false; out.scrollTop = out.scrollHeight; }
    if (!c.running) return; // a command the console started and the engine does not count as a run
    if (!stale) setText($('#state-line'), 'Running ' + name + ' · ' + Math.round(c.elapsed) + ' s');
    const stop = $('.levers [data-action="stop"]');
    if (stop) stop.hidden = false;
  }

  // -- refetch: the page holds the previous render and swaps what changed, node by node -------
  const KEEP = new Set(['output', 'notice', 'live-note']);
  const OWN_ATTRS = new Set(['open', 'aria-current', 'aria-pressed', 'aria-sort']);
  const OWN_CLASSES = ['changed', 'graded', 'near', 'stale'];
  let refetching = false;
  async function refetch() {
    if (refetching || !live) return;
    refetching = true;
    try {
      const r = await fetch(location.pathname + location.search, { cache: 'no-store', headers: { 'X-Bodh': 'console' } });
      if (!r.ok) throw new Error('status ' + r.status);
      const fresh = new DOMParser().parseFromString(await r.text(), 'text/html');
      const g = fresh.querySelector('meta[name="bodh-generation"]');
      if (g) generation = g.content;
      const s = fresh.querySelector('#stamp');
      if (s && stamp) { stamp.dataset.asOf = s.dataset.asOf; asOf = Date.parse(s.dataset.asOf); }
      morph($('main'), fresh.querySelector('main'));
      sentence = $('#state-line') ? $('#state-line').textContent : sentence;
      afterRender();
      clock();
      if (list.band !== defaultBand) loadRows(list.band, 0);
    } catch (e) { warn('refetch', e); } finally { refetching = false; }
  }
  function morph(a, b) {
    if (!a || !b) return;
    if (a.nodeType === Node.TEXT_NODE) { if (a.data !== b.data) { a.data = b.data; wash(a.parentNode); } return; }
    if (a.nodeType !== Node.ELEMENT_NODE || KEEP.has(a.id)) return;
    const ours = (name) => OWN_ATTRS.has(name) || name === 'class';
    Array.from(a.attributes).forEach((at) => { if (!b.hasAttribute(at.name) && !ours(at.name)) a.removeAttribute(at.name); });
    Array.from(b.attributes).forEach((at) => { if (!ours(at.name) && a.getAttribute(at.name) !== at.value) a.setAttribute(at.name, at.value); });
    if (a.className !== b.className) {
      const own = OWN_CLASSES.filter((c) => a.classList.contains(c));
      a.setAttribute('class', b.getAttribute('class') || ''); own.forEach((c) => a.classList.add(c));
    }
    if (a.id === 'list-rows') return morphRows(a, b);
    const own = (n) => n.nodeType === Node.ELEMENT_NODE && n.classList.contains('verdicts'); // built here, not by the server
    const ac = Array.from(a.childNodes).filter((n) => !own(n)), bc = Array.from(b.childNodes);
    const same = ac.length === bc.length && ac.every((n, i) => n.nodeType === bc[i].nodeType && n.nodeName === bc[i].nodeName);
    if (!same) { a.replaceChildren(...bc.map((n) => document.importNode(n, true))); wash(a); return; }
    ac.forEach((n, i) => morph(n, bc[i]));
  }
  function morphRows(a, b) { // rows are matched by article, so a row keeps its menu and its grade
    const have = new Map($$('.list-row', a).map((r) => [r.dataset.id, r]));
    const next = $$('.list-row', b).map((r) => { const old = have.get(r.dataset.id); if (old) { morph(old, r); return old; } const n = document.importNode(r, true); wash(n); return n; });
    a.replaceChildren(...next);
  }

  // -- the list: band from the server (console) or by hiding (export); folder and sort here ----
  const bandLinks = () => $$('.filters [data-band]');
  const chosen = bandLinks().find((a) => a.getAttribute('aria-current') === 'true');
  const defaultBand = chosen ? Number(chosen.dataset.band) : NaN;
  const list = { band: defaultBand, folder: '', sort: 'score' };
  function applyList() {
    const ul = $('#list-rows');
    if (!ul) return;
    const rows = $$('.list-row', ul);
    rows.forEach((r) => { r.hidden = Number(r.dataset.score) < list.band || (!!list.folder && r.dataset.folder !== list.folder); });
    const by = list.sort === 'newest'
      ? (x, y) => (y.dataset.arrived > x.dataset.arrived ? 1 : y.dataset.arrived < x.dataset.arrived ? -1 : 0)
      : (x, y) => Number(y.dataset.score) - Number(x.dataset.score);
    rows.slice().sort(by).forEach((r) => ul.append(r));
    bandLinks().forEach((a) => a.setAttribute('aria-current', Number(a.dataset.band) === list.band ? 'true' : 'false'));
    $$('.filters [data-sort]').forEach((a) => a.setAttribute('aria-current', a.dataset.sort === list.sort ? 'true' : 'false'));
    const sel = $('.filters select[data-folder]');
    if (sel) sel.value = list.folder;
  }
  async function loadRows(band, offset) {
    const ul = $('#list-rows');
    if (!ul) return;
    try {
      const d = await (await fetch('/list?band=' + band + '&offset=' + offset, { cache: 'no-store' })).json();
      const tpl = document.createElement('template');
      tpl.innerHTML = d.rows; // the server's own rendering of its own rows, as on first paint
      if (!offset) ul.replaceChildren();
      ul.append(...tpl.content.childNodes);
      const loaded = $$('.list-row', ul).length, more = $('[data-action="list_more"]');
      setText($('#list-count'), 'Showing ' + count(loaded) + ' of ' + count(d.total) + '.');
      if (more) more.hidden = loaded >= d.total;
      menus(); applyList();
    } catch (e) { warn('list', e); say('The console did not answer.', true); }
  }
  document.addEventListener('click', (ev) => {
    const band = ev.target.closest('.filters [data-band]'), sort = ev.target.closest('.filters [data-sort]');
    if (!band && !sort) return;
    ev.preventDefault();
    if (band) {
      list.band = Number(band.dataset.band);
      if (live) return loadRows(list.band, 0); // the server has the rows below the page's band
    } else list.sort = sort.dataset.sort;
    applyList();
  });
  document.addEventListener('change', (ev) => { if (ev.target.matches('.filters select[data-folder]')) { list.folder = ev.target.value; applyList(); } });

  // -- the feeds table: a filter box and sortable headers -----------------------------------
  const feeds = { q: '', col: -1, dir: 'ascending' };
  function applyFeeds() {
    const table = $('.feeds-table');
    if (!table) return;
    const rows = $$('tbody tr', table);
    rows.forEach((tr) => { tr.hidden = !!feeds.q && !tr.firstElementChild.textContent.toLowerCase().includes(feeds.q); });
    const ths = $$('thead th', table);
    ths.forEach((th, i) => th.setAttribute('aria-sort', i === feeds.col ? feeds.dir : 'none'));
    if (feeds.col < 0) return;
    const numeric = ths[feeds.col].classList.contains('num');
    const key = (tr) => {
      const text = tr.children[feeds.col].textContent.trim(), n = parseFloat(text.replace(/[^0-9.-]/g, ''));
      return !numeric ? text.toLowerCase() : isNaN(n) ? -Infinity : n;
    };
    const sign = feeds.dir === 'ascending' ? 1 : -1;
    rows.slice().sort((x, y) => { const a = key(x), b = key(y); return (a > b ? 1 : a < b ? -1 : 0) * sign; }).forEach((tr) => tr.parentNode.append(tr));
  }
  document.addEventListener('input', (ev) => { if (ev.target.matches('input[data-filter="feeds"]')) { feeds.q = ev.target.value.trim().toLowerCase(); applyFeeds(); } });
  document.addEventListener('click', (ev) => {
    const b = ev.target.closest('.feeds-table th[aria-sort] button');
    if (!b) return;
    const col = Number(b.dataset.sort);
    feeds.dir = feeds.col === col && feeds.dir === 'ascending' ? 'descending' : 'ascending';
    feeds.col = col; applyFeeds();
  });

  // -- tooltips: one node, values first; the spend chart snaps to the nearest day -----------
  const tip = document.createElement('div');
  tip.className = 'tooltip'; tip.hidden = true; tip.setAttribute('role', 'status'); document.body.append(tip);
  function showTip(el) {
    const text = el && el.dataset.tip;
    if (!text) return hideTip();
    const r = el.getBoundingClientRect();
    tip.textContent = text; tip.hidden = false;
    tip.style.left = (r.left + r.width / 2 + window.scrollX) + 'px';
    tip.style.top = (r.top + window.scrollY) + 'px';
  }
  function hideTip() { tip.hidden = true; $$('.columns .col.near').forEach((c) => c.classList.remove('near')); }
  document.addEventListener('mouseover', (ev) => { if (!ev.target.closest('svg.columns')) showTip(ev.target.closest('[data-tip]')); });
  document.addEventListener('mouseout', (ev) => { if (ev.target.closest('[data-tip]') && !ev.target.closest('svg.columns')) hideTip(); });
  document.addEventListener('focusin', (ev) => showTip(ev.target.closest('[data-tip]'))); document.addEventListener('focusout', hideTip);
  document.addEventListener('mousemove', (ev) => {
    const svg = ev.target.closest('svg.columns');
    if (!svg) return;
    let best = null, gap = Infinity;
    $$('rect.col', svg).forEach((c) => { const r = c.getBoundingClientRect(); const d = Math.abs(ev.clientX - (r.left + r.width / 2)); if (d < gap) { gap = d; best = c; } });
    $$('.col.near', svg).forEach((c) => { if (c !== best) c.classList.remove('near'); });
    if (best) { best.classList.add('near'); showTip(best); }
  });
  document.addEventListener('mouseleave', (ev) => { if (ev.target.matches && ev.target.matches('svg.columns')) hideTip(); }, true);

  // -- the banner: a dismissal is remembered by its key -------------------------------------
  function applyBanner() {
    const b = $('#attention .banner');
    let seen = null;
    try { seen = localStorage.getItem('bodh.dismissed'); } catch (e) { seen = null; }
    if (b && b.querySelector('[data-dismiss]') && seen === b.dataset.key) b.hidden = true;
  }
  document.addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-dismiss]') && ev.target.closest('.banner');
    if (!b) return;
    try { localStorage.setItem('bodh.dismissed', b.dataset.key || ''); } catch (e) { warn('storage', e); }
    b.hidden = true;
  });

  // -- the nav follows the reading: the last section whose top has passed mid-screen ---------
  const links = $$('.nav a[data-section]');
  function navCurrent() {
    const mid = window.innerHeight / 2;
    let current = null;
    links.forEach((a) => { const s = document.getElementById(a.dataset.section); if (s && s.getBoundingClientRect().top <= mid) current = a; });
    links.forEach((a) => a.setAttribute('aria-current', a === (current || links[0]) ? 'true' : 'false'));
  }
  if (links.length) { window.addEventListener('scroll', () => requestAnimationFrame(navCurrent), { passive: true }); navCurrent(); }

  // -- the levers, the other actions, their confirmations -----------------------------------
  const post = (url, payload) => fetch(url, { method: 'POST', headers: HEADERS, body: JSON.stringify(payload) }).then((r) => r.json());
  function params() {
    const p = {};
    $$('.more [data-param]').forEach((el) => { p[el.dataset.param] = el.type === 'checkbox' ? el.checked : el.value; });
    return p;
  }
  async function act(action, label) {
    say('Working: ' + label + '…');
    try {
      const d = await post('/api/action', Object.assign({ action }, params()));
      say(d.message || (d.ok ? 'Done.' : 'It failed.'), !d.ok);
      if (d.output !== undefined) applyChild({ args: [action.replace(/_/g, ' ')], running: false, code: d.ok ? 0 : 1, output: (d.output || '(no output)').split('\n') });
      refetch();
    } catch (e) { warn('action', e); say('The console did not answer.', true); }
  }
  function confirmDialog(text, label) {
    return new Promise((resolve) => {
      const dlg = document.createElement('dialog'), p = document.createElement('p'), row = document.createElement('div');
      const no = button('Cancel', 'quiet'), yes = button(label, 'primary');
      p.textContent = text; row.className = 'levers'; row.append(no, yes); dlg.append(p, row); document.body.append(dlg);
      const done = (ok) => { dlg.close(); dlg.remove(); resolve(ok); };
      no.addEventListener('click', () => done(false)); yes.addEventListener('click', () => done(true));
      dlg.addEventListener('cancel', (ev) => { ev.preventDefault(); done(false); });
      if (typeof dlg.showModal === 'function') { dlg.showModal(); yes.focus(); } else done(window.confirm(text));
    });
  }
  async function confirmed(action, label) {
    try {
      const q = new URLSearchParams(Object.assign({ action }, params()));
      const d = await (await fetch('/api/plan?' + q, { cache: 'no-store' })).json();
      if (!d.ok) return say(d.message || 'It cannot run now.', true);
      if (await confirmDialog(d.sentence, d.button)) act(action, label);
    } catch (e) { warn('plan', e); say('The console did not answer.', true); }
  }
  document.addEventListener('click', async (ev) => {
    const b = ev.target.closest('button[data-action]');
    if (!b) return;
    const action = b.dataset.action, label = b.textContent.trim();
    if (action === 'list_more') return loadRows(list.band, $$('#list-rows .list-row').length);
    if (action === 'log_more') {
      return fetch('/api/log?n=200', { cache: 'no-store' }).then((r) => r.json())
        .then((d) => { setText($('#log'), d.lines.join('\n') || '(the log is empty)'); b.hidden = true; }).catch((e) => warn('log', e));
    }
    if (b.dataset.confirm) return confirmed(action, label);
    act(action, label);
  });

  // -- the verdicts: a menu built per row from its words, a key per grade on a focused row ----
  const graded = new Map(); // article id -> the word given this session, kept across refetches
  function pressed(row) {
    const word = graded.get(row.dataset.id);
    $$('button[data-verdict]', row).forEach((o) => { if (o.dataset.verdict === word) o.setAttribute('aria-pressed', 'true'); else o.removeAttribute('aria-pressed'); });
    row.classList.toggle('graded', !!word);
  }
  function menus() {
    $$('.list-row[data-verdicts]').forEach((row) => {
      if (!$('.verdicts', row)) {
        const box = document.createElement('div');
        box.className = 'verdicts';
        row.dataset.verdicts.split(' ').forEach((word) => {
          const key = Object.keys(KEYS).find((k) => KEYS[k] === word);
          box.append(button(word, 'quiet small', { 'data-verdict': word, title: key ? word + ' (' + key + ')' : word }));
        });
        row.append(box);
      }
      pressed(row);
    });
  }
  document.addEventListener('click', async (ev) => {
    const b = ev.target.closest('button[data-verdict]');
    if (!b) return;
    const row = b.closest('.list-row');
    try {
      const d = await post('/api/mark', { id: row.dataset.id, kind: b.dataset.verdict });
      say(d.message || (d.ok ? 'Recorded.' : 'It failed.'), !d.ok);
      if (!d.ok) return;
      graded.set(row.dataset.id, b.dataset.verdict);
      pressed(row);
    } catch (e) { warn('mark', e); say('The console did not answer.', true); }
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.metaKey || ev.ctrlKey || ev.altKey || ev.target.matches('input, select, textarea')) return;
    const row = ev.target.closest('.list-row');
    const word = row && KEYS[ev.key];
    const b = word && $('button[data-verdict="' + word + '"]', row);
    if (b) { ev.preventDefault(); b.click(); }
  });

  // -- events, with polling as the fallback ----------------------------------------------
  let failures = 0, source = null;
  function connect() {
    if (!live) return;
    if (!window.EventSource) return poll();
    try { source = new EventSource('/api/events'); } catch (e) { warn('events', e); return poll(); }
    source.addEventListener('tick', (ev) => { try { onTick(JSON.parse(ev.data)); } catch (e) { warn('tick', e); } });
    source.onopen = () => { failures = 0; note(''); };
    source.onerror = () => { if (++failures >= 3) { source.close(); poll(); } };
  }
  function poll() {
    cycle = 30000;
    note('Live updates are unavailable here; the page checks every 30 s.');
    const once = () => fetch('/api/tick', { cache: 'no-store' }).then((r) => r.json()).then(onTick).catch((e) => warn('poll', e));
    once();
    setInterval(once, cycle);
  }

  function afterRender() { menus(); applyList(); applyFeeds(); applyBanner(); applyChild(lastChild); }
  afterRender(); connect();
})();
