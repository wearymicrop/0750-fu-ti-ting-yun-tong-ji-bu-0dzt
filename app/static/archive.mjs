import {$, el, label, stamp, table} from './format.mjs';
import {request} from './api.mjs';

export function drawArchived(report, revision) {
  $('archive-actions').hidden = !report;
  $('archive-detail').replaceChildren();
  if (!report) return;
  $('download').href = `/api/runs/${report.id}/export`;
  const rows = [
    ['当前结论', report.id],
    ['结论', revision ? (revision.formal ? `正式修订 · 第 ${revision.generation} 次` : '未定稿') : '原班报'],
    ['规则', report.manifest.rule_version],
    ['摘要', report.manifest.digest],
  ];
  if (revision) rows.splice(1, 0, ['原班报', revision.root_id], ['父级', revision.parent_id]);
  for (const [key, value] of rows) $('archive-detail').append(el('dt', key), el('dd', value, 'mono'));
}

export function setReviseEnabled(headId, reportId) {
  const button = $('revise');
  button.disabled = !reportId || (headId != null && headId !== reportId);
  button.title = button.disabled ? '请先打开最新正式结论再修订' : '';
}

export function drawChain(report, chain, open) {
  const list = $('revision-chain');
  if (!report) {
    list.replaceChildren();
    return;
  }
  list.replaceChildren();
  const nodes = chain?.chain?.length ? chain.chain : [];
  for (const node of nodes) {
    const item = el('li', null, node.id === report.id ? 'current' : '');
    const link = el('button', `第 ${node.generation} 次 · ${node.kind === 'report' ? '原班报' : '修订'} · ${node.id.slice(0, 12)}`, 'mono link');
    link.type = 'button';
    link.title = node.id;
    link.addEventListener('click', () => open(node.id));
    item.append(link);
    if (node.status) item.append(el('span', label(node.status), 'tag'));
    list.append(item);
  }
  if (!nodes.length) list.append(el('li', '暂无修订', 'empty'));
}

export async function drawArchive(open) {
  const {runs} = await request('/api/runs');
  table($('archive-table'), [['班报'], ['来源'], ['结论'], ['对账状态']], runs.map(run => {
    const link = el('button', run.id.slice(0, 12), 'mono');
    link.type = 'button';
    link.addEventListener('click', () => open(run.id));
    const kind = run.kind === 'revision' ? `修订 · 第 ${run.generation} 次` : '原班报';
    return [link, label(run.manifest.source), kind, label(run.status)];
  }));
}
