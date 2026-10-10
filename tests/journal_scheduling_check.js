// Run with: node tests/journal_scheduling_check.js
// Synthetic DOM contract tests; no browser, working database or network requests.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Option {
  constructor(text, value) { this.textContent = text; this.value = String(value); this.dataset = {}; }
}
function element(value = '') {
  let storedValue = String(value);
  return { get value() { return storedValue; }, set value(next) { storedValue = String(next); }, textContent: '', readOnly: false, dataset: {},
    classList: { add() {}, remove() {}, toggle() {} }, addEventListener() {} };
}
function select(options) {
  const el = element(options[0]?.value || '');
  el.options = options;
  el.add = option => el.options.push(option);
  el.replaceChildren = (...children) => { el.options = children; el.value = children[0]?.value || ''; };
  Object.defineProperty(el, 'selectedIndex', { get: () => el.options.findIndex(o => o.value === String(el.value)) });
  return el;
}
const lessons = [
  { id: 1, date: '2026-09-05', number: 1, topic: 'Synthetic A', homework: '', type: 'ORDINARY', notes: '', time: '', nz_bound: true, homework_target: '' },
  { id: 2, date: '2026-10-09', number: 1, topic: 'Synthetic B', homework: '', type: 'ORDINARY', notes: '', time: '', nz_bound: true, homework_target: '' },
  { id: 3, date: '2027-02-01', number: 1, topic: 'Synthetic C', homework: '', type: 'ORDINARY', notes: '', time: '', nz_bound: false, homework_target: '' },
];
const elements = {};
for (const id of ['create_lesson_date', 'create_lesson_number', 'insert_before_id', 'insert_before_hint',
  'plan_topic_id_input', 'lesson_topic_input', 'lesson_hw_input', 'edit_date_input', 'edit_date_hint',
  'edit_number_input', 'edit_time_input', 'edit_lesson_id', 'edit_lesson_date', 'edit_lesson_number',
  'edit_lesson_topic', 'edit_lesson_hw', 'edit_lesson_type', 'edit_lesson_notes', 'swap_first_id',
  'swap_first_label', 'createLessonModal', 'editLessonModal', 'swapTopicsModal']) elements[id] = element();
elements['journal-lesson-data'] = { textContent: JSON.stringify(lessons) };
const planned = new Option('4. Synthetic planned theme', '4');
planned.dataset = { title: 'Synthetic planned theme', hw: 'Synthetic homework', date: '2026-11-03', type: 'QUIZ' };
elements.plan_topic_select = select([new Option('Choose', ''), planned]);
elements.create_homework_target = select([]);
elements.edit_homework_target = select([]);
elements.swap_second_id = select([]);
elements.createLessonForm = { reset() { elements.plan_topic_id_input.value = ''; }, elements: { lesson_type: element('ORDINARY') } };
elements.editLessonForm = { getAttribute: () => '/journal/10/lesson/0/edit/' };
const context = {
  document: { addEventListener() {}, getElementById: id => elements[id] || null },
  Option, console, setTimeout() {}, window: {},
  URLSearchParams,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/journal.js'), 'utf8'), context);
let checks = 0;
function check(expression, description) { assert.ok(expression, description); checks++; }
elements.create_lesson_date.value = '2026-10-09';
context.openModal('createLessonModal');
check(elements.create_lesson_number.value === '2', 'next number on selected date');
check(elements.create_homework_target.options.some(o => o.textContent.includes('01.02.2027')), 'future dated homework choice');
check(!elements.create_homework_target.options.some(o => o.value === 'date:2026-09-05'), 'past lesson day excluded');
check(elements.create_homework_target.options.some(o => o.value === 'date:2026-10-09'), 'selected lesson day included');
check(elements.create_homework_target.options.filter(o => o.value.startsWith('date:')).map(o => o.value).join(',') === 'date:2026-10-09,date:2026-11-03,date:2027-02-01', 'lesson and plan days sorted chronologically');
check(elements.create_homework_target.options.filter(o => o.value.startsWith('date:')).every(o => /^\d{2}\.\d{2}\.\d{4}$/.test(o.textContent)), 'homework list displays dates');
context.closeModal('createLessonModal');
context.insertBeforeLesson(2);
check(elements.create_lesson_number.value === '1', 'insert before number');
check(elements.insert_before_id.value === '2', 'insert before identity');
context.closeModal('createLessonModal');
check(elements.insert_before_id.value === '', 'normal creation resets insertion');
context.createFromPlan(4);
check(elements.create_lesson_date.value === '2026-11-03', 'planned date filled');
check(elements.lesson_topic_input.value === 'Synthetic planned theme', 'planned topic filled');
check(elements.createLessonForm.elements.lesson_type.value === 'QUIZ', 'planned type filled');
context.openEditLessonModalById(1);
check(elements.edit_date_input.readOnly, 'online date locked');
check(elements.editLessonForm.action === '/journal/10/lesson/1/edit/', 'journal 10 preserved in action URL');
context.openEditLessonModalById(3);
check(!elements.edit_date_input.readOnly, 'local date editable');
context.openSwapTopics(1);
check(elements.swap_second_id.options.length === 3, 'swap contains two alternative lessons and empty choice');
context.fillHomeworkChoices(elements.edit_homework_target, '2026-09-05', 1, 1, 'date:2026-09-14');
check(elements.edit_homework_target.value === 'date:2026-09-14', 'imported homework due date preserved');
check(context.escapeJournalText('<img src=x onerror="alert(1)">') === '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;', 'sync report escapes executable HTML');
check(context.escapeJournalText('A & B') === 'A &amp; B', 'sync report preserves literal punctuation');
context.fillHomeworkChoices(elements.edit_homework_target, '2026-09-05', 1, 1, 'lesson:2');
check(elements.edit_homework_target.value === 'date:2026-10-09', 'existing lesson assignment selects its date');
lessons.push({ ...lessons[1], id: 4, number: 2 });
lessons.push({ ...lessons[1], id: 5, date: '2026-11-10', type: 'THEMATIC' });
elements['journal-lesson-data'].textContent = JSON.stringify(lessons);
context.fillHomeworkChoices(elements.edit_homework_target, '2026-10-09', 3, 2, 'date:2026-10-09');
check(elements.edit_homework_target.options.filter(o => o.value === 'date:2026-10-09').length === 1, 'same-day lessons produce one day choice');
check(!elements.edit_homework_target.options.some(o => o.value === 'date:2026-11-10'), 'summary-only day excluded from homework choices');
context.fillHomeworkChoices(elements.edit_homework_target, '2026-11-03', 1, 2);
check(elements.edit_homework_target.value === 'next' && !elements.edit_homework_target.options.some(o => o.value === 'date:2026-10-09'), 'moving source date resets an earlier assignment');
for (const id of ['lesson_repeat_frequency', 'lesson_repeat_interval', 'lesson_repeat_end',
  'lesson_repeat_count', 'lesson_repeat_until', 'lessonRepeatOptions', 'lessonRepeatIntervalField',
  'lessonRepeatCountField', 'lessonRepeatUntilField', 'lessonRepeatPreview', 'lessonCreateSubmit']) elements[id] = element();
elements.createLessonForm.dataset = { yearStart: '2026-09-01', yearEnd: '2027-06-30' };
elements.create_lesson_date.value = '2026-10-09';
elements.lesson_repeat_frequency.value = 'weekly';
elements.lesson_repeat_end.value = 'count';
elements.lesson_repeat_count.value = '4';
elements.lesson_repeat_interval.value = '3';
elements.lesson_repeat_until.value = '2026-11-07';
context.updateLessonRecurrence();
check(context.lessonRepeatDates().dates.join(',') === '2026-10-09,2026-10-16,2026-10-23,2026-10-30', 'weekly preview includes first lesson');
check(!elements.lessonRepeatOptions.hidden && !elements.lesson_repeat_count.disabled && elements.lesson_repeat_count.required, 'weekly count controls enabled');
check(elements.lessonRepeatIntervalField.hidden && elements.lesson_repeat_interval.disabled && elements.lesson_repeat_until.disabled, 'unused controls disabled');
check(!elements.lessonCreateSubmit.disabled && elements.lessonCreateSubmit.textContent === 'Створити уроки (4)', 'submit displays number of occurrences');
check(elements.lessonRepeatPreview.textContent.includes('09.10.2026, 16.10.2026, 23.10.2026, 30.10.2026'), 'preview displays readable dates');
check(elements.create_homework_target.options.some(o => o.value === 'date:2026-10-16'), 'new series days available for homework selection');
elements.lesson_repeat_frequency.value = 'biweekly';
elements.lesson_repeat_end.value = 'date';
context.updateLessonRecurrence();
check(context.lessonRepeatDates().dates.join(',') === '2026-10-09,2026-10-23,2026-11-06', 'biweekly end date inclusive and not required to be lesson weekday');
check(elements.lessonRepeatCountField.hidden && elements.lesson_repeat_count.disabled && !elements.lesson_repeat_until.disabled && elements.lesson_repeat_until.required, 'end date controls replace count');
check(elements.lesson_repeat_until.min === '2026-10-09', 'end date minimum follows first lesson');
elements.lesson_repeat_until.value = '2026-10-09';
context.updateLessonRecurrence();
check(context.lessonRepeatDates().dates.join(',') === '2026-10-09' && !elements.lessonCreateSubmit.disabled, 'end date equal to first date permits one lesson');
elements.lesson_repeat_frequency.value = 'custom';
elements.lesson_repeat_end.value = 'count';
elements.lesson_repeat_count.value = '3';
context.updateLessonRecurrence();
check(context.lessonRepeatDates().dates.join(',') === '2026-10-09,2026-10-30,2026-11-20', 'custom interval preserved across daylight saving change');
check(!elements.lessonRepeatIntervalField.hidden && !elements.lesson_repeat_interval.disabled && elements.lesson_repeat_interval.required, 'custom interval input enabled');
elements.lesson_repeat_interval.value = '0';
context.updateLessonRecurrence();
check(elements.lessonCreateSubmit.disabled && context.lessonRepeatDates().error, 'invalid custom interval blocks creation');
elements.lesson_repeat_frequency.value = 'weekly';
elements.lesson_repeat_count.value = '2';
elements.create_lesson_date.value = '2026-12-25';
context.updateCreateLessonFields();
check(context.lessonRepeatDates().dates.join(',') === '2026-12-25,2027-01-01', 'dates cross calendar year correctly');
elements.create_lesson_date.value = '2027-06-25';
context.updateCreateLessonFields();
check(elements.lessonCreateSubmit.disabled && context.lessonRepeatDates().dates.length === 0, 'series beyond academic year blocked');
elements.create_lesson_date.value = '9999-12-31';
context.updateCreateLessonFields();
check(elements.lessonCreateSubmit.disabled, 'out of year start rejected before calculating overflow dates');
elements.create_lesson_date.value = '2026-10-09';
elements.lesson_repeat_count.value = '101';
context.updateLessonRecurrence();
check(elements.lessonCreateSubmit.disabled, 'more than 100 lessons blocked');
elements.lesson_repeat_count.value = '1';
context.updateLessonRecurrence();
check(elements.lessonCreateSubmit.disabled, 'count mode requires at least two lessons');
elements.lesson_repeat_frequency.value = 'none';
context.updateLessonRecurrence();
check(elements.lessonRepeatOptions.hidden && elements.lesson_repeat_count.disabled && elements.lesson_repeat_until.disabled && elements.lesson_repeat_end.disabled, 'turning off recurrence disables all additional submitted fields');
check(!elements.lessonCreateSubmit.disabled && elements.lessonCreateSubmit.textContent === 'Зберегти урок', 'single lesson remains available after invalid series');
check(!elements.create_homework_target.options.some(o => o.value === 'date:2026-10-16'), 'turning off recurrence removes uncreated future day choices');
for (const id of ['create_lesson_type', 'createLessonOrdinaryFields', 'createLessonSummaryFields',
  'createLessonNumberField', 'create_lesson_date_label', 'createLessonModalTitle', 'create_lesson_time',
  'create_summary_title', 'create_summary_calculate']) elements[id] = element();
elements.createLessonForm.dataset.assessmentUrl = '/journal/10/assessment/';
elements.createLessonForm.reportValidity = () => true;
elements.create_lesson_type.value = 'THEMATIC';
elements.create_lesson_date.value = '2026-10-09';
elements.create_lesson_time.value = '14:30';
elements.create_summary_title.value = 'Synthetic <topic> & name';
elements.lesson_repeat_frequency.value = 'weekly';
elements.lesson_repeat_count.value = '101';
context.updateCreateLessonFields();
check(elements.createLessonOrdinaryFields.hidden && elements.createLessonOrdinaryFields.disabled, 'summary mode excludes all ordinary fields from validation and submission');
check(!elements.createLessonSummaryFields.hidden && !elements.createLessonSummaryFields.disabled, 'summary fields enabled');
check(elements.createLessonNumberField.hidden && elements.create_lesson_number.disabled && !elements.create_lesson_number.required, 'daily number unavailable for summary column');
check(elements.insert_before_id.disabled && elements.insert_before_hint.hidden, 'summary does not submit insertion operation');
check(elements.create_lesson_date_label.textContent.includes('Відображати колонку') && elements.create_lesson_date.max === '2027-06-30', 'summary date is placement date within academic year');
check(elements.createLessonModalTitle.textContent === 'Додати тематичне оцінювання' && elements.lessonCreateSubmit.textContent === 'Створити колонку' && !elements.lessonCreateSubmit.disabled, 'summary is available despite an inactive invalid recurrence');
const summaryParams = new URLSearchParams(elements.create_summary_calculate.href.split('?')[1]);
check(summaryParams.get('grade_type') === 'THEMATIC' && summaryParams.get('date') === '2026-10-09' && summaryParams.get('lesson_time') === '14:30' && summaryParams.get('title') === 'Synthetic <topic> & name', 'calculation link preserves and safely encodes only summary parameters');
check(context.validateSummaryCalculation(), 'valid summary can open calculation');
elements.createLessonForm.reportValidity = () => false;
check(!context.validateSummaryCalculation(), 'invalid summary fields prevent calculation navigation');
elements.create_lesson_type.value = 'ANNUAL';
context.updateCreateLessonFields();
check(elements.createLessonModalTitle.textContent === 'Додати річну оцінку' && new URLSearchParams(elements.create_summary_calculate.href.split('?')[1]).get('grade_type') === 'ANNUAL', 'annual mode preserves summary-specific fields');
elements.create_lesson_type.value = 'QUIZ';
elements.lesson_repeat_frequency.value = 'none';
context.updateCreateLessonFields();
check(!elements.createLessonOrdinaryFields.hidden && !elements.createLessonOrdinaryFields.disabled && elements.createLessonSummaryFields.hidden && elements.createLessonSummaryFields.disabled, 'ordinary mode restored after summary');
check(!elements.create_lesson_number.disabled && elements.create_lesson_number.required && elements.lessonCreateSubmit.textContent === 'Зберегти урок', 'ordinary numbering and submit restored');
const summaryPlan = new Option('Synthetic planned thematic', '9');
summaryPlan.dataset = { title: 'Synthetic planned thematic', hw: '', date: '2026-10-23', type: 'THEMATIC' };
elements.plan_topic_select.add(summaryPlan);
elements.createLessonForm.elements.lesson_type = elements.create_lesson_type;
context.createFromPlan(9);
check(elements.create_lesson_type.value === 'THEMATIC' && elements.create_summary_title.value === 'Synthetic planned thematic' && elements.plan_topic_id_input.value === '9', 'planned summary opens the correct form and retains matching plan');
elements.create_lesson_type.value = 'SEMESTER_1';
context.updateCreateLessonFields();
check(elements.plan_topic_id_input.value === '', 'changing summary type clears an incompatible plan link');

function listenable(value) {
  const input = element(value);
  input.handlers = {};
  input.addEventListener = function (event, handler) { this.handlers[event] = handler; };
  input.label = { hidden: false };
  input.closest = () => input.label;
  return input;
}
for (const [id, value] of [['id_grade_type', 'THEMATIC'], ['id_source', 'current'],
  ['id_start_date', '2026-09-01'], ['id_end_date', '2026-10-09']]) elements[id] = listenable(value);
const assessmentItems = [
  { id: '11', type: 'ORDINARY', date: '2026-09-05' },
  { id: '12', type: 'TEST', date: '2026-10-09' },
  { id: '13', type: 'QUIZ', date: '2026-10-16' },
  { id: '14', type: 'THEMATIC', date: '2026-10-09' },
  { id: '15', type: 'SEMESTER_1', date: '2026-10-09' },
  { id: '16', type: 'SEMESTER_2', date: '2026-10-09' },
  { id: 'legacy:THEMATIC', type: 'THEMATIC' },
];
const assessmentTypes = ['ORDINARY', 'QUIZ', 'TEST', 'THEMATIC', 'SEMESTER_1', 'SEMESTER_2', 'ANNUAL', 'ADJUSTED'].map(value => {
  const input = listenable(value);
  input.checked = ['ORDINARY', 'QUIZ', 'TEST'].includes(value);
  return input;
});
const assessmentLessons = assessmentItems.map(item => {
  const input = listenable(item.id);
  input.checked = item.id === '12';
  return input;
});
elements['assessment-source-data'] = { textContent: JSON.stringify(assessmentItems) };
elements.id_target_lesson = select([new Option('New column', ''), new Option('Thematic', '14'), new Option('Semester', '15')]);
elements.id_target_lesson.value = '15';
context.document.querySelectorAll = selector => selector.includes('name="source_types"') ? assessmentTypes : selector.includes('name="lesson_ids"') ? assessmentLessons : [];
context.initAssessmentSources();
check(assessmentTypes.find(i => i.value === 'THEMATIC').disabled && assessmentTypes.find(i => i.value === 'THEMATIC').label.hidden, 'current assessments show teaching lesson types only');
check(assessmentLessons.find(i => i.value === '13').disabled && assessmentLessons.find(i => i.value === '14').disabled, 'themes outside dates or selected grade source unavailable');
check(!assessmentLessons.find(i => i.value === '11').checked && assessmentLessons.find(i => i.value === '12').checked, 'initialization preserves the teacher selection');
check(elements.id_target_lesson.value === '' && elements.id_target_lesson.options[2].disabled, 'incompatible target column cleared');
assessmentTypes.find(i => i.value === 'ORDINARY').checked = false;
assessmentTypes.find(i => i.value === 'ORDINARY').handlers.change();
check(assessmentLessons.find(i => i.value === '11').disabled && !assessmentLessons.find(i => i.value === '11').checked && assessmentLessons.find(i => i.value === '12').checked, 'type filter limits selected topics');
elements.id_end_date.value = '2026-10-16';
elements.id_end_date.handlers.change();
check(assessmentLessons.find(i => i.value === '13').checked && !assessmentLessons.find(i => i.value === '13').disabled, 'date change updates eligible topics');
elements.id_grade_type.value = 'SEMESTER_1';
elements.id_grade_type.handlers.change.call(elements.id_grade_type);
check(elements.id_source.value === 'thematic' && assessmentTypes.find(i => i.value === 'THEMATIC').checked && assessmentTypes.find(i => i.value === 'TEST').disabled, 'semester defaults to thematic grades');
check(assessmentLessons.find(i => i.value === '14').checked && !assessmentLessons.find(i => i.value === 'legacy:THEMATIC').checked, 'dated thematic selected while undated legacy requires explicit selection');
context.selectAssessmentLessons(true);
check(assessmentLessons.find(i => i.value === 'legacy:THEMATIC').checked && !assessmentLessons.find(i => i.value === '12').checked, 'select all includes only eligible sources');
elements.id_grade_type.value = 'ANNUAL';
elements.id_grade_type.handlers.change.call(elements.id_grade_type);
check(elements.id_source.value === 'semester' && assessmentLessons.find(i => i.value === '15').checked && assessmentLessons.find(i => i.value === '16').checked && assessmentLessons.find(i => i.value === '14').disabled, 'annual defaults to semester grades');
elements.id_source.value = 'all';
elements.id_source.handlers.change();
check(assessmentTypes.every(i => !i.disabled && i.checked), 'explicit all source permits all known assessment types');
const schoolTimeSelect = select([new Option('No time', ''), new Option('Lesson 1', '08:30'), new Option('Lesson 2', '09:25')]);
const originalAdd = schoolTimeSelect.add;
schoolTimeSelect.add = option => { option.remove = () => { schoolTimeSelect.options = schoolTimeSelect.options.filter(item => item !== option); }; originalAdd(option); };
context.selectSchoolLessonTime(schoolTimeSelect, '08:30');
check(schoolTimeSelect.value === '08:30', 'configured school time is selected');
context.selectSchoolLessonTime(schoolTimeSelect, '11:40');
check(schoolTimeSelect.value === '' && schoolTimeSelect.options.length === 3, 'unlisted time is not added for a new lesson');
context.selectSchoolLessonTime(schoolTimeSelect, '11:40', true);
check(schoolTimeSelect.value === '11:40' && schoolTimeSelect.options[3].dataset.legacy, 'recorded historical time can be preserved while editing');
context.selectSchoolLessonTime(schoolTimeSelect, '09:25', true);
check(schoolTimeSelect.value === '09:25' && schoolTimeSelect.options.length === 3, 'historical option is cleared when another lesson is edited');
console.log(`PASS: ${checks} JavaScript scheduling checks`);
