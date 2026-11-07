(() => {
  const root = document.querySelector('[data-login-default]');
  if (!root) return;
  const tabs = [...root.querySelectorAll('[data-login-tab]')];
  const panels = [...root.querySelectorAll('[data-login-panel]')];
  function select(name, focus = false) {
    tabs.forEach(tab => {
      const chosen = tab.dataset.loginTab === name;
      tab.setAttribute('aria-selected', String(chosen));
      tab.tabIndex = chosen ? 0 : -1;
      if (chosen && focus) tab.focus();
    });
    panels.forEach(panel => {
      panel.setAttribute('role','tabpanel');
      panel.hidden = panel.dataset.loginPanel !== name;
    });
  }
  root.querySelector('.login-tabs').hidden = false;
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => select(tab.dataset.loginTab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = tabs.length - 1;
      else return;
      event.preventDefault(); select(tabs[next].dataset.loginTab, true);
    });
  });
  select(tabs.some(tab => tab.dataset.loginTab === root.dataset.loginDefault) ? root.dataset.loginDefault : 'nz');
})();
