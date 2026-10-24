(() => {
  'use strict';
  const csrf = document.querySelector('#connection-csrf input[name=csrfmiddlewaretoken]')?.value;
  async function check(card) {
    if (card.getAttribute('aria-busy') === 'true') return;
    card.setAttribute('aria-busy', 'true');
    const button = card.querySelector('[data-check-connection]');
    const original = button.textContent;
    button.disabled = true; button.textContent = 'Перевіряємо…';
    try {
      const response = await fetch(card.dataset.checkUrl, {method: 'POST', credentials: 'same-origin',
        headers: {'X-CSRFToken': csrf, 'Accept': 'application/json'}});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) {
        throw new Error('Не вдалося перевірити підключення. Оновіть сторінку або увійдіть знову.');
      }
      const data = await response.json();
      const state = data.service;
      card.querySelectorAll('[data-field]').forEach(element => {
        const name = element.dataset.field;
        if (!(name in state)) return;
        let value = state[name];
        if (name === 'can_export' || name === 'can_import') value = value === true ? 'Дозволено' : value === false ? 'Немає дозволу' : 'Ще не перевірено';
        if (name === 'status_label') {
          value = `${state.ready ? '🟢' : '🔴'} ${value}`;
          element.classList.toggle('is-online', state.ready);
        }
        element.textContent = value || (name === 'active_url' ? 'Ще не перевірена' : '');
        if (name === 'error') element.hidden = !value;
      });
    } catch (error) {
      const message = card.querySelector('[data-field=error]');
      message.textContent = error.message; message.hidden = false;
    } finally {card.removeAttribute('aria-busy'); button.disabled = false; button.textContent = original;}
  }
  document.querySelectorAll('[data-connection-card]').forEach(card => {
    card.querySelector('[data-check-connection]').addEventListener('click', () => check(card));
    // Only connection metadata is requested. No pupil/grade synchronization.
    check(card);
  });

  const wizard = document.querySelector('[data-connection-wizard]');
  if (!wizard) return;
  const steps = [...wizard.querySelectorAll('[data-wizard-step]')];
  const back = wizard.querySelector('[data-wizard-back]');
  const next = wizard.querySelector('[data-wizard-next]');
  const submit = wizard.querySelector('[data-wizard-submit]');
  const firstError = wizard.querySelector('.errorlist');
  let current = firstError ? Math.max(0, steps.findIndex(step => step.contains(firstError))) : 0;
  if (firstError && !steps.some(step => step.contains(firstError))) current = 1;
  wizard.querySelectorAll('details').forEach(details => {if (details.querySelector('.errorlist')) details.open = true;});
  function show() {
    steps.forEach((step, index) => {step.hidden = index !== current;});
    document.querySelectorAll('[data-step-label]').forEach((label, index) => {
      if (index === current) label.setAttribute('aria-current', 'step');
      else label.removeAttribute('aria-current');
    });
    back.hidden = current === 0; next.hidden = current === steps.length - 1;
    submit.hidden = current !== steps.length - 1;
    const source = wizard.querySelector('[name=source]');
    const profile = wizard.querySelector('[name=profile]');
    const address = wizard.querySelector('[name=address]');
    wizard.querySelector('[data-wizard-summary]').textContent = `${source.selectedOptions[0]?.textContent || ''} · ${address.value || profile.selectedOptions[0]?.textContent || 'Вкажіть адресу'}`;
  }
  next.addEventListener('click', () => {
    const inputs = [...steps[current].querySelectorAll('input,select')];
    if (current === 0) {
      const address = wizard.querySelector('[name=address]');
      const profile = wizard.querySelector('[name=profile]');
      address.setCustomValidity(!address.value.trim() && !profile.value ? 'Вкажіть адресу або оберіть мережу.' : '');
    }
    const invalid = inputs.find(input => !input.checkValidity());
    if (invalid) {invalid.reportValidity(); return;}
    current++; show();
    steps[current].querySelector('input,select')?.focus({preventScroll: true});
  });
  back.addEventListener('click', () => {current--; show();});
  wizard.addEventListener('input', () => wizard.querySelector('[name=address]').setCustomValidity(''));
  wizard.addEventListener('change', () => wizard.querySelector('[name=address]').setCustomValidity(''));
  wizard.addEventListener('invalid', event => {
    const index = steps.findIndex(step => step.contains(event.target));
    if (index >= 0) current = index;
    event.target.closest('details')?.setAttribute('open', '');
    show();
  }, true);
  wizard.addEventListener('submit', event => {
    const invalid = [...wizard.querySelectorAll('input,select')].find(input => !input.checkValidity());
    if (invalid) {
      event.preventDefault();
      current = steps.findIndex(step => step.contains(invalid));
      if (current < 0) current = 0;
      show(); invalid.reportValidity(); return;
    }
    submit.disabled = true; wizard.querySelector('[data-wizard-status]').textContent = 'Перевіряємо адресу та права ключа…';
  });
  show();
})();
