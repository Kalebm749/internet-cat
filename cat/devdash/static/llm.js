(() => {
  const update = () => document.querySelectorAll(".elapsed[data-started]").forEach(el => {
    if (el.closest(".llm-stale")) return;
    const seconds = Math.max(0, Date.now() / 1000 - Number(el.dataset.started));
    el.textContent = `${seconds.toFixed(1)} s elapsed`;
  });
  update();
  setInterval(update, 500);
})();
