'use strict';
// The top-of-screen banners: shows what /api/banners hands it, in the server's words. Each slides down
// from the top and leaves by itself after 5 seconds, or sooner when swiped up or clicked; while the
// pointer is on it it stays, like a banner held on an iPhone.

const SHOWN = 5000;
const AT_ONCE = 3;  // the others wait for a place: a banner is not to cover the game
const LEAVING = 300;
const FAILURES = 10;  // polls without an answer before this concludes the program is gone
const bridge = window.bannerWindow;
const list = document.getElementById('banners');
const queue = [];
let pumping = false;

const fit = () => bridge.fit(list.offsetHeight);

function leave(card) {
  if (card.classList.contains('out')) return;
  clearTimeout(card.timer);
  card.classList.add('out');
  setTimeout(() => {
    card.remove();
    if (list.children.length || queue.length) fit();
    if (!list.children.length && !queue.length) bridge.hide();
    pump();
  }, LEAVING);
}

function arm(card) {
  clearTimeout(card.timer);
  card.timer = setTimeout(() => leave(card), SHOWN);
}

function build(item) {
  const card = document.createElement('article');
  card.className = 'banner';
  const icon = document.createElement('img');
  icon.src = '/ui/icon.ico';
  icon.alt = '';
  const text = document.createElement('div');
  text.className = 'text';
  const head = document.createElement('div');
  head.className = 'head';
  const title = document.createElement('b');
  title.textContent = item.title || '';
  const when = document.createElement('time');
  when.textContent = '现在';
  head.append(title, when);
  const body = document.createElement('p');
  body.textContent = item.body || '';
  text.append(head, body);
  card.append(icon, text);

  card.addEventListener('mouseenter', () => clearTimeout(card.timer));
  card.addEventListener('mouseleave', () => { if (!card.classList.contains('dragging')) arm(card); });
  let start = null;
  card.addEventListener('pointerdown', (event) => {
    start = event.clientY;
    card.setPointerCapture(event.pointerId);
    card.classList.add('dragging');
  });
  card.addEventListener('pointermove', (event) => {
    if (start !== null) card.style.setProperty('--drag', `${Math.min(0, event.clientY - start)}px`);
  });
  card.addEventListener('pointerup', (event) => {
    if (start === null) return;
    const moved = event.clientY - start;
    start = null;
    card.classList.remove('dragging');
    card.style.removeProperty('--drag');
    if (Math.abs(moved) < 5) bridge.open();
    if (Math.abs(moved) < 5 || moved < -24) leave(card);  // clicked, or swiped up
  });
  return card;
}

async function pump() {
  if (pumping) return;
  pumping = true;
  try {
    while (queue.length && list.children.length < AT_ONCE) {
      const item = queue.shift();
      if (typeof item.expires === 'number' && item.expires * 1000 <= Date.now()) continue;
      const card = build(item);
      list.append(card);
      await fit();  // the window is on the screen before the banner moves
      void card.offsetHeight;
      card.classList.add('in');
      arm(card);
    }
  } finally {
    pumping = false;
  }
}

async function poll() {
  let after = -1, failures = 0;
  for (;;) {
    try {
      const response = await fetch(`/api/banners?after=${after}`);
      if (!response.ok) throw new Error(response.status);
      const value = await response.json();
      failures = 0;
      if (!value.on) return bridge.quit();
      after = value.after;
      queue.push(...value.items);
      pump();
    } catch {
      failures += 1;
      if (failures >= FAILURES) return bridge.quit();
      await new Promise((done) => setTimeout(done, 3000));
    }
  }
}

poll();
