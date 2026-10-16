// Applies the console theme before the first paint (no flash of the wrong one). Same rules as
// src/theme.ts: the viewer's saved choice, or their system's preference.
;(function () {
  var choice = 'system'
  try {
    choice = localStorage.getItem('console-theme') || 'system'
  } catch (e) {}
  var dark =
    choice === 'dark' ||
    (choice !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.classList.toggle('dark', dark)
})()
