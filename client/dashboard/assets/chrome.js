// Shared page chrome for /dash and /chat: fetch helpers, the light/dark
// toggle, the top-right menu, and inlining the logo + theme icon (as
// <svg>, not <img>, so they inherit currentColor across theme flips).

const $ = (id) => document.getElementById(id);
const j = async (p) => (await fetch(p)).json();

const wsUrl = (path) =>
  `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${path}`;

// getRandomValues (unlike randomUUID) works on plain http, which is how
// the dashboard is reached over a LAN/tailscale address.
function randomHex(bytes = 16) {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('');
}

// --- light/dark theme: explicit choice persists; otherwise follows the OS ---
const themeBtn = $('themeBtn');
function readStoredTheme() {
  try { return localStorage.getItem('clique-theme'); } catch (e) { return null; }
}
function writeStoredTheme(t) {
  try { localStorage.setItem('clique-theme', t); } catch (e) {}
}
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
}
applyTheme(readStoredTheme()
  || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'));
themeBtn.addEventListener('click', () => {
  const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  writeStoredTheme(next);
  applyTheme(next);
});

// --- top-right menu ---
const menuBtn = $('menuBtn'), menuPanel = $('menuPanel');
function setMenu(open) {
  menuPanel.hidden = !open;
  menuBtn.setAttribute('aria-expanded', String(open));
}
menuBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  setMenu(menuPanel.hidden);
});
document.addEventListener('click', (e) => {
  if (!menuPanel.hidden && !menuPanel.contains(e.target) && e.target !== menuBtn) setMenu(false);
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setMenu(false); });

fetch('/assets/logo.svg').then(r => r.text()).then(t => { $('logo').innerHTML = t; })
  .catch(console.error);
fetch('/assets/light_dark.svg').then(r => r.text()).then(t => { themeBtn.innerHTML = t; })
  .catch(console.error);
