/**
 * RepostFlow — app.js
 * Lightweight vanilla JavaScript for the dashboard.
 */

(function () {
  'use strict';

  /* ---- Sidebar toggle on mobile ---- */
  const sidebar    = document.getElementById('rfSidebar');
  const openBtn    = document.getElementById('sidebarOpen');
  const closeBtn   = document.getElementById('sidebarClose');

  if (openBtn && sidebar) {
    openBtn.addEventListener('click', () => sidebar.classList.add('open'));
  }
  if (closeBtn && sidebar) {
    closeBtn.addEventListener('click', () => sidebar.classList.remove('open'));
  }

  /* ---- Auto-dismiss flash messages ---- */
  document.querySelectorAll('.rf-flash').forEach(function (el) {
    setTimeout(function () {
      var bsAlert = bootstrap.Alert.getOrCreateInstance(el);
      if (bsAlert) bsAlert.close();
    }, 6000);
  });

  /* ---- Active nav link highlighting ---- */
  // Already handled server-side via Jinja2, but keep a client-side fallback.
  var currentPath = window.location.pathname;
  document.querySelectorAll('.rf-nav-link').forEach(function (link) {
    if (link.getAttribute('href') === currentPath) {
      link.classList.add('active');
    }
  });

})();
