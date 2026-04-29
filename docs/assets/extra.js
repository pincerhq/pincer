/* Pincer theme — small JS enhancements */

document$.subscribe(function () {

  /* ── Smooth anchor scroll ──────────────────────────────── */
  document.querySelectorAll('a[href^="#"]').forEach(function (anchor) {
    anchor.addEventListener('click', function (e) {
      const target = document.querySelector(this.getAttribute('href'));
      if (target) {
        e.preventDefault();
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
  });

  /* ── Add "back to top" pulse on long pages ─────────────── */
  const backToTop = document.querySelector('.md-top');
  if (backToTop) {
    backToTop.style.setProperty('--pincer-pulse', '#28C864');
  }

  /* ── Annotate external links ───────────────────────────── */
  document.querySelectorAll('.md-typeset a[href^="http"]').forEach(function (link) {
    if (!link.hostname.includes(window.location.hostname)) {
      link.setAttribute('target', '_blank');
      link.setAttribute('rel', 'noopener noreferrer');
    }
  });
});
