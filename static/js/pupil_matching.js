(() => {
  const search = document.querySelector('[data-pupil-search]');
  if (!search) return;
  const rows = [...document.querySelectorAll('[data-pupil-row]')];
  search.addEventListener('input', () => {
    const query = search.value.trim().toLocaleLowerCase('uk');
    let shown = 0;
    rows.forEach(row => {
      const label = row.querySelector('label')?.textContent || '';
      row.hidden = !label.toLocaleLowerCase('uk').includes(query);
      if (!row.hidden) shown++;
    });
    document.querySelector('[data-pupil-search-status]').textContent = `Показано: ${shown} з ${rows.length}. Вибрані відповідності збережені у формі.`;
  });
})();
