import {$, buckets, el, label, num, rate, stamp, table} from './format.mjs';

const REASONS = ['SAFETY', 'POWER', 'MECHANICAL', 'MAINTENANCE'];
const STATUSES = ['approved', 'pending', 'denied'];

const local = value => {
  if (!value) return '';
  const text = String(value);
  const match = text.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::(\d{2}))?(?:Z|[+-]\d{2}:\d{2})?$/);
  if (!match) return '';
  const date = new Date(text);
  const pad = number => String(number).padStart(2, '0');
  if (!Number.isNaN(date.getTime())) {
    const shifted = new Date(date.getTime() + 8 * 3600_000);
    return `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}T`
      + `${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}:${pad(shifted.getUTCSeconds())}`;
  }
  return `${match[1]}:${match[2] ?? '00'}`;
};

function field(name, value, placeholder, type = 'text') {
  const input = el('input');
  input.type = type;
  input.name = name;
  input.placeholder = placeholder;
  input.value = value ?? '';
  if (type === 'datetime-local') input.step = '1';
  return input;
}

function select(name, options, value) {
  const node = el('select');
  node.name = name;
  for (const [key, text] of options) {
    const option = el('option', text);
    option.value = key ?? '';
    if ((value ?? '') === (key ?? '')) option.selected = true;
    node.append(option);
  }
  return node;
}

function row(className, fields, onRemove) {
  const entry = el('div', null, className);
  entry.append(...fields);
  const remove = el('button', '删除', 'row-remove');
  remove.type = 'button';
  remove.addEventListener('click', () => { entry.remove(); onRemove?.(); });
  entry.append(remove);
  return entry;
}

function correctionRow(values = {}, onChange) {
  const fields = [
    field('device', values.device, '设备号'),
    field('id', values.id, '报码'),
    field('start', local(values.start_local ?? values.start), '开始', 'datetime-local'),
    field('end', local(values.end_local ?? values.end), '恢复（可空）', 'datetime-local'),
    select('reason', REASONS.map(key => [key, label(key)]), values.reason),
  ];
  const entry = row('revision-row', fields, onChange);
  entry.dataset.kind = 'correction';
  return entry;
}

function permitRow(values = {}, onChange) {
  const fields = [
    field('device', values.device, '设备号'),
    field('id', values.id, '审批编号'),
    field('start', local(values.start_local ?? values.start), '开始', 'datetime-local'),
    field('end', local(values.end_local ?? values.end), '结束', 'datetime-local'),
    select('reason', [[null, '全部原因'], ...REASONS.map(key => [key, label(key)])], values.reason),
    select('status', STATUSES.map(key => [key, label(key)]), values.status ?? 'approved'),
    field('approval', values.approval, '审批凭据'),
  ];
  const entry = row('revision-row', fields, onChange);
  entry.dataset.kind = 'permit';
  return entry;
}

const moment = value => value ? `${value.length === 16 ? `${value}:00` : value}+08:00` : null;

function collect(container) {
  const rows = [...container.children].filter(node => node.dataset.kind);
  const pick = node => Object.fromEntries(
    [...node.querySelectorAll('input, select')].map(input => [input.name, input.value.trim() || null]),
  );
  const corrections = rows.filter(node => node.dataset.kind === 'correction').map(pick)
    .filter(value => value.device || value.id || value.start)
    .map(value => ({device: value.device, id: value.id, start: moment(value.start),
                    end: moment(value.end), reason: value.reason}));
  const permits = rows.filter(node => node.dataset.kind === 'permit').map(pick)
    .filter(value => value.device || value.id || value.start || value.end)
    .map(value => ({device: value.device, id: value.id, start: moment(value.start),
                    end: moment(value.end), reason: value.reason, status: value.status,
                    approval: value.approval}));
  return {corrections, permits};
}

export function drawRevisionForm(parent, onSubmit) {
  $('revision-form-panel').hidden = false;
  $('revision-review').hidden = true;
  $('revision-parent-label').textContent = parent.kind === 'revision'
    ? `父级修订 · 第 ${parent.generation} 次` : '原班报';
  $('revision-parent-label').title = parent.id;
  $('revision-state').textContent = '未提交';
  $('revision-feedback').textContent = '';
  $('revision-note').value = '';
  $('correction-rows').replaceChildren(correctionRow());
  $('permit-rows').replaceChildren(permitRow());
  $('add-correction').onclick = () => $('correction-rows').append(correctionRow());
  $('add-permit').onclick = () => $('permit-rows').append(permitRow());
  $('revision-check').onclick = () => onSubmit(false);
  $('revision-save').onclick = () => onSubmit(true);
  $('revision-save').disabled = false;
  $('revision-check').disabled = false;
  $('revision-form-panel').scrollIntoView({block: 'start'});
}

export function readRevisionForm() {
  const {corrections, permits} = collect($('correction-rows'));
  const body = {};
  if (corrections.length) body.corrections = corrections;
  const permitsOnly = collect($('permit-rows')).permits;
  if (permitsOnly.length) body.permits = permitsOnly;
  const note = $('revision-note').value.trim();
  if (note) body.note = note;
  return body;
}

export function revisionFeedback(message, tone) {
  $('revision-feedback').textContent = message;
  $('revision-feedback').dataset.tone = tone ?? '';
}

export function revisionState(text) {
  $('revision-state').textContent = text;
}

export function closeRevisionForm() {
  $('revision-form-panel').hidden = true;
}

const PROBLEMS = {
  SERVICE_CHANGED: '营业秒数变动',
  DEVICE_SET_CHANGED: '设备集合变动',
  SECONDS_NOT_CONSERVED: '四类秒数不相抵',
  DEVICE_UNBALANCED: '该设备差额不为零',
  TOTAL_SERVICE_CHANGED: '合计营业秒数变动',
  TOTAL_UNBALANCED: '合计差额不为零',
  TOTAL_NOT_CONSERVED: '合计四类秒数不相抵',
};

export function drawRevision(node, {preview = false} = {}) {
  const {diff} = node;
  if (!diff) return;
  $('revision-review').hidden = false;
  if (!preview) $('revision-form-panel').hidden = true;
  $('revision-balance').textContent = preview ? '试算 · 未保存' : (node.formal ? '正式结论' : '未定稿');
  $('revision-balance').dataset.tone = diff.check.balanced ? '' : 'warn';

  const adopted = el('table');
  const head = adopted.createTHead().insertRow();
  for (const title of ['设备', '报码', '原行', '原原因', '新原因', '新时段', '新状态']) {
    const cell = el('th', title);
    cell.scope = 'col';
    head.append(cell);
  }
  const body = adopted.createTBody();
  for (const row of diff.adopted) {
    const entry = body.insertRow();
    const before = row.before[0];
    entry.append(
      el('td', row.device), el('td', row.id), el('td', row.base_lines.join('、')),
      el('td', label(before?.reason)), el('td', label(row.value.reason)),
      el('td', `${stamp(row.value.start)} → ${row.value.end == null ? '未恢复' : stamp(row.value.end)}`),
      el('td', label(row.after?.state)),
    );
  }
  $('adopted-codes').replaceChildren(diff.adopted.length
    ? adopted : el('p', '本次修订没有采用报码更正', 'empty'));

  $('adopted-permits').replaceChildren();
  table($('adopted-permits'), [['设备'], ['审批'], ['凭据'], ['状态'], ['时段'], ['计入秒数', true], ['说明']],
    diff.permits.map(row => [
      row.device, row.id, row.approval ?? '—', label(row.status),
      `${stamp(row.start)} → ${stamp(row.end)}`, num(row.applied_seconds),
      row.duplicate_of != null ? `与原班报第 ${row.duplicate_of} 行重复，未重复计入` : '已采用',
    ]));

  const totals = diff.totals;
  const deviceRows = Object.entries(diff.by_device).map(([device, row]) => [
    device, num(row.parent.service), num(row.revision.service), num(row.delta.service),
    num(row.parent.responsible), num(row.revision.responsible), num(row.delta.responsible),
    num(row.parent.exempt), num(row.revision.exempt), num(row.delta.exempt),
    num(row.parent.difference), num(row.revision.difference),
  ]);
  deviceRows.push(['合计', num(totals.parent.service), num(totals.revision.service), num(totals.delta.service),
    num(totals.parent.responsible), num(totals.revision.responsible), num(totals.delta.responsible),
    num(totals.parent.exempt), num(totals.revision.exempt), num(totals.delta.exempt),
    num(totals.parent.difference), num(totals.revision.difference)]);
  table($('revision-devices'), [
    ['设备'], ['原营业', true], ['新营业', true], ['Δ', true],
    ['原责任', true], ['新责任', true], ['Δ', true],
    ['原豁免', true], ['新豁免', true], ['Δ', true],
    ['原差额', true], ['新差额', true],
  ], deviceRows);

  const changes = diff.segments.map(row => [
    row.device, `${stamp(row.start)} → ${stamp(row.end)}`, num(row.seconds),
    row.parent ? `${buckets[row.parent.bucket]} · ${label(row.parent.code)}` : '—',
    row.revision ? `${buckets[row.revision.bucket]} · ${label(row.revision.code)}` : '—',
  ]);
  const moved = ['responsible', 'exempt', 'missing'].map(key => [buckets[key], num(totals.delta[key])]);
  const summary = el('div', null, 'diff-summary');
  summary.append(
    el('span', `片段变更 ${changes.length} 段`),
    ...moved.map(([name, value]) => el('span', `${name} ${value.startsWith('-') ? '' : '+'}${value}`)),
    el('span', `可用率 ${rate(totals.parent.availability_bps ?? totals.parent.lower_bound_bps)} → ${rate(totals.revision.availability_bps ?? totals.revision.lower_bound_bps)}`),
  );
  const wrapper = el('div');
  wrapper.append(summary);
  const grid = el('div', 'table-scroll inner');
  table(grid, [['设备'], ['时段'], ['秒数', true], ['修订前'], ['修订后']], changes);
  wrapper.append(grid);
  if (!diff.check.balanced) {
    const problems = el('ul', null, 'balance-problems');
    for (const item of diff.check.problems) {
      problems.append(el('li', `${item.device ?? '合计'} · ${PROBLEMS[item.why] ?? item.why}`));
    }
    wrapper.append(el('p', '未对平，不能作为正式结论', 'balance-warning'), problems);
  }
  $('revision-diff').replaceChildren(wrapper);

  const chain = el('p', null, 'revision-meta');
  chain.append(`修订 ${node.id.slice(0, 12)} · 第 ${node.generation} 次 · 父级 ${node.parent_id.slice(0, 12)} · `);
  chain.append(`输入摘要 ${node.manifest.input_sha256.slice(0, 12)} · 结果摘要 ${node.manifest.digest.slice(0, 12)}`);
  $('revision-diff').append(chain);
}
