// Synthetic DOM checks for the floating menu, including viewport and keyboard.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const listeners = {};
const trigger = {
  attributes: {}, focused: false,
  setAttribute(key, value) { this.attributes[key] = value; },
  getBoundingClientRect() { return { left: 45, top: 570, bottom: 595 }; },
  focus() { this.focused = true; },
};
const action = { focused: false, focus() { this.focused = true; } };
const menu = { style: { display: 'none' }, offsetWidth: 300, offsetHeight: 120,
  querySelector() { return action; } };
const body = { children: [], appendChild(el) { this.children.push(el); el.parentNode = this; } };
const document = {
  body,
  getElementById(id) { return id === 'studentMenu_1' ? menu : null; },
  querySelectorAll() { return [menu]; },
  addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
};
const window = { innerWidth: 320, innerHeight: 600,
  addEventListener(type, fn) { (listeners[type] ||= []).push(fn); } };
const context = { document, window, console, setTimeout() {}, URLSearchParams };
vm.createContext(context);
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname, '../static/js/journal.js'), 'utf8'), context);
let checks = 0;
function check(ok, message) { assert.ok(ok, message); checks++; }
const event = { currentTarget: trigger, stopPropagation() {} };
context.toggleStudentActionMenu(event, 'studentMenu_1');
check(menu.parentNode === body, 'menu escapes table clipping and sticky stacking');
check(menu.style.display === 'block', 'menu opens');
check(trigger.attributes['aria-expanded'] === 'true', 'open state exposed to assistive technology');
check(menu.style.top === '450px', 'last-row menu opens above trigger');
check(menu.style.left === '12px', 'menu stays within narrow viewport');
check(action.focused, 'keyboard focus enters menu');
const inside = { target: { closest(selector) { return selector === '.student-action-dropdown' ? menu : null; } } };
listeners.click.forEach(fn => fn(inside));
check(menu.style.display === 'block', 'click inside body-mounted menu does not close it');
listeners.keydown.forEach(fn => fn({ key: 'Escape' }));
check(menu.style.display === 'none' && trigger.focused, 'Escape closes and restores focus');
check(trigger.attributes['aria-expanded'] === 'false', 'closed state exposed');
context.toggleStudentActionMenu(event, 'studentMenu_1');
listeners.scroll.forEach(fn => fn({ target: { closest() { return null; } } }));
check(menu.style.display === 'none', 'table scroll closes detached menu');
context.toggleStudentActionMenu(event, 'studentMenu_1');
listeners.resize.forEach(fn => fn());
check(menu.style.display === 'none', 'viewport resize closes detached menu');
context.toggleStudentActionMenu(event, 'studentMenu_1');
context.toggleStudentActionMenu(event, 'studentMenu_1');
check(menu.style.display === 'none', 'second click toggles menu closed');
context.toggleStudentActionMenu(event, 'studentMenu_1');
listeners.click.forEach(fn => fn({ target: { closest() { return null; } } }));
check(menu.style.display === 'none', 'outside click closes menu');
console.log(`Student menu: ${checks} checks passed`);
