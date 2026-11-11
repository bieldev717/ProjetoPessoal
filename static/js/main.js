/* ═══════════════════════════════════════════
   main.js — FinançasPro
   Funções utilitárias compartilhadas
═══════════════════════════════════════════ */

// ── FORMATADORES ────────────────────────────

/**
 * Formata número como moeda brasileira: R$ 1.234,56
 */
function fmt(valor) {
  if (valor === null || valor === undefined || valor === '') return 'R$ —';
  return 'R$ ' + Number(valor).toLocaleString('pt-BR', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });
}

function safeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
}

function safeColor(value) {
  return /^#[0-9a-f]{3,8}$/i.test(String(value ?? '')) ? value : '#6ee7b7';
}

/**
 * Formata data ISO (2024-05-28) para exibição (28/05/2024)
 */
function fmtData(dataStr) {
  if (!dataStr) return '—';
  const [y, m, d] = dataStr.split('-');
  return `${d}/${m}/${y}`;
}

/**
 * Retorna nome do mês em português
 */
const MESES = [
  'Janeiro','Fevereiro','Março','Abril','Maio','Junho',
  'Julho','Agosto','Setembro','Outubro','Novembro','Dezembro'
];
function nomeMes(n) { return MESES[(n-1)] || ''; }

// ── TOAST ────────────────────────────────────

let toastTimer = null;

/**
 * Exibe notificação temporária
 * @param {string} msg    - Mensagem
 * @param {'success'|'error'} tipo
 */
function showToast(msg, tipo = 'success') {
  const el = document.getElementById('toast');
  if (!el) return;

  el.textContent = msg;
  el.className = `toast ${tipo} show`;

  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.classList.remove('show');
  }, 3000);
}

// ── SIDEBAR MOBILE ───────────────────────────

function toggleSidebar() {
  const sidebar  = document.getElementById('sidebar');
  const overlay  = document.getElementById('sidebarOverlay');
  const isOpen   = sidebar.classList.toggle('open');
  overlay.classList.toggle('open', isOpen);
  document.body.style.overflow = isOpen ? 'hidden' : '';
}

// Fecha sidebar ao navegar (SPA-like behavior no mobile)
document.querySelectorAll('.nav-item').forEach(link => {
  link.addEventListener('click', () => {
    const sidebar = document.getElementById('sidebar');
    if (sidebar.classList.contains('open')) toggleSidebar();
  });
});

// ── KEYBOARD SHORTCUTS ───────────────────────

document.addEventListener('keydown', e => {
  // ESC fecha modais
  if (e.key === 'Escape') {
    if (typeof fecharModal === 'function') fecharModal();
    const sidebar = document.getElementById('sidebar');
    if (sidebar?.classList.contains('open')) toggleSidebar();
  }

  // Ctrl+N abre modal de nova transação (na página de transações)
  if ((e.ctrlKey || e.metaKey) && e.key === 'n') {
    if (typeof abrirModal === 'function') {
      e.preventDefault();
      abrirModal();
    }
  }
});

// ── HELPERS ──────────────────────────────────

function safeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}

function safeColor(value) {
  return /^#[0-9a-f]{6}$/i.test(String(value)) ? value : '#94a3b8';
}

/**
 * Debounce simples
 */
function debounce(fn, delay = 300) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), delay);
  };
}

/**
 * Clamp valor entre min e max
 */
function clamp(v, min, max) { return Math.min(Math.max(v, min), max); }

// ── INIT ─────────────────────────────────────

// Destaca item ativo do nav com base na URL
(function highlightNav() {
  const path = window.location.pathname;
  document.querySelectorAll('.nav-item').forEach(el => {
    const href = el.getAttribute('href');
    el.classList.toggle('active', href === path || (href !== '/' && path.startsWith(href)));
  });
})();
