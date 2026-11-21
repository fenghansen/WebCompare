(() => {
  let saved;
  try { saved=localStorage.getItem('wc-theme'); } catch {}
  const theme=saved==='light'||saved==='dark'?saved:(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');
  document.documentElement.dataset.theme=theme;
  document.querySelector('meta[name=theme-color]').setAttribute('content',theme==='dark'?'#08111f':'#f3f7fc');
})();
