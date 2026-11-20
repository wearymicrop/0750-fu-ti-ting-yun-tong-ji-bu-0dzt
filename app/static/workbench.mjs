import {$, stamp} from './format.mjs';
import {request} from './api.mjs';
import {drawOverview} from './reconcile.mjs';
import {drawEvidence} from './evidence.mjs';
import {drawStage} from './stages.mjs';
import {drawArchive, drawArchived, drawChain, setReviseEnabled} from './archive.mjs';
import {
  closeRevisionForm, drawRevision, drawRevisionForm, readRevisionForm, revisionFeedback, revisionState,
} from './revision.mjs';

const links = [...document.querySelectorAll('[data-section]')];
const state = {sample: 'dirty', result: null, report: null, revision: null, chain: null, evidenceKey: 0};
const status = value => { $('status').textContent = value; };

function evidenceSource() {
  if (state.report && state.report.result === state.result) return `/api/runs/${state.report.id}`;
  return `/api/preview/${state.sample}`;
}

async function inspect(segment) {
  const key = ++state.evidenceKey;
  const args = new URLSearchParams({device: segment.device, start: segment.start});
  try {
    const data = await request(`${evidenceSource()}/evidence?${args}`);
    if (key === state.evidenceKey) drawEvidence(data);
  } catch (error) {
    if (key === state.evidenceKey) $('evidence').textContent = error.message;
  }
}

function syncView() {
  const active = links.find(link => link.dataset.section === window.location.hash.slice(1)) ?? links[0];
  const section = active.dataset.section;
  for (const link of links) {
    if (link === active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  $('page-title').textContent = active.textContent.trim();
  $('overview').hidden = section !== 'reconcile';
  $('archive').hidden = section !== 'runs';
  $('stage').hidden = ['reconcile', 'runs'].includes(section);
  if (section === 'runs') {
    drawArchived(state.report, state.revision);
    drawChain(state.report, state.chain, openReport);
    setReviseEnabled(state.chain?.head_id, state.report?.id);
    drawArchive(openReport).catch(error => status(error.message));
  } else if (state.result) {
    if (section === 'reconcile') drawOverview(state.result, inspect);
    else drawStage(section, state.result);
  }
}

function showResult(result) {
  state.evidenceKey++;
  state.result = result;
  $('range').textContent = `${stamp(result.window.start)} → ${stamp(result.window.end)}`;
  syncView();
}

let loadKey = 0;
async function loadSample() {
  const key = ++loadKey;
  const sample = $('sample').value;
  status('载入中');
  $('save').disabled = true;
  closeRevision();
  try {
    const result = await request(`/api/preview/${sample}`);
    if (key !== loadKey) return;
    state.sample = sample;
    state.report = null;
    state.revision = null;
    state.chain = null;
    showResult(result);
    status('样例已计算');
  } catch (error) {
    if (key === loadKey) {
      $('sample').value = state.sample;
      status(error.message);
    }
  } finally {
    if (key === loadKey) $('save').disabled = !state.result;
  }
}

async function saveReport(body) {
  const report = await request('/api/runs', body);
  state.report = report;
  state.revision = null;
  state.chain = await request(`/api/runs/${report.id}/revisions`);
  showResult(report.result);
  status(report.reused ? '已复用同一班报' : '班报已保存');
}

function closeRevision() {
  closeRevisionForm();
  $('revision-review').hidden = true;
}

async function openReport(id) {
  try {
    const node = await request(`/api/runs/${id}`);
    state.report = node;
    state.revision = node.kind === 'revision' ? node : null;
    if (node.kind === 'report') {
      state.sample = node.manifest.source;
      $('sample').value = state.sample;
    }
    $('run-feedback').textContent = '';
    state.chain = await request(`/api/runs/${id}/revisions`);
    showResult(node.result);
    if (state.revision) drawRevision(node);
    else closeRevision();
    status('已读取归档');
  } catch (error) { status(error.message); }
}

async function submitRevision(persist) {
  if (!state.report) return;
  const body = readRevisionForm();
  if (!body.corrections && !body.permits) {
    revisionFeedback('修订须至少包含一条更正或一份审批', 'warn');
    return;
  }
  $('revision-save').disabled = true;
  $('revision-check').disabled = true;
  revisionState(persist ? '提交中' : '试算中');
  try {
    const query = persist ? '' : '?dry_run=true';
    const node = await request(`/api/runs/${state.report.id}/revisions${query}`, body);
    if (node.already_applied) {
      revisionFeedback('这些更正已包含在结论中，未重复叠加时长', 'warn');
      revisionState('已存在');
    } else {
      revisionFeedback(persist ? (node.reused ? '已复用同一修订' : '修订已保存为正式结论') : '试算对平，可保存为正式结论', '');
      revisionState(persist ? '已保存' : '试算');
    }
    drawRevision(node, {preview: !persist});
    if (persist) {
      await openReport(node.id);
      window.location.hash = 'runs';
    }
  } catch (error) {
    const detail = error.details?.problems
      ? `：${error.details.problems.map(item => item.why).join('、')}` : '';
    revisionFeedback(`${error.message}${detail}`, 'warn');
    revisionState('未通过');
  } finally {
    $('revision-save').disabled = false;
    $('revision-check').disabled = false;
  }
}

$('sample').addEventListener('change', loadSample);
window.addEventListener('hashchange', syncView);
$('save').addEventListener('click', async () => {
  if (state.report) {
    window.location.hash = 'runs';
    return;
  }
  if (state.sample === 'custom') {
    window.location.hash = 'runs';
    return;
  }
  $('save').disabled = true;
  $('sample').disabled = true;
  $('upload-button').disabled = true;
  try {
    await saveReport({sample: state.sample});
    window.location.hash = 'runs';
  } catch (error) { status(error.message); }
  finally {
    $('save').disabled = false;
    $('sample').disabled = false;
    $('upload-button').disabled = false;
  }
});
$('upload-button').addEventListener('click', () => $('upload').click());
$('upload').addEventListener('change', async () => {
  const file = $('upload').files[0];
  if (!file) return;
  $('sample').disabled = true;
  $('save').disabled = true;
  $('upload-button').disabled = true;
  try {
    if (file.size > 2_000_000) throw new Error('文件须小于 2 MB');
    const data = JSON.parse(await file.text());
    loadKey++;
    await saveReport({data});
    state.sample = 'custom';
    $('sample').value = 'custom';
    window.location.hash = 'reconcile';
  } catch (error) {
    status(error instanceof SyntaxError ? 'JSON 格式有误，请修正后重选' : error.message);
  } finally {
    $('upload').value = '';
    $('sample').disabled = false;
    $('save').disabled = !state.result;
    $('upload-button').disabled = false;
  }
});
$('revise').addEventListener('click', () => {
  if (!state.report) return;
  if (state.chain?.head_id && state.chain.head_id !== state.report.id) {
    status('该班报已有更新的正式结论，请先打开最新结论');
    return;
  }
  drawRevisionForm(state.report, submitRevision);
  revisionFeedback('填写后先试算核对，再保存为正式结论');
});
$('rerun').addEventListener('click', async () => {
  if (!state.report) return;
  $('rerun').disabled = true;
  try {
    await request(`/api/runs/${state.report.id}/rerun`, {});
    $('run-feedback').textContent = '重跑一致';
  } catch (error) { $('run-feedback').textContent = error.message; }
  finally { $('rerun').disabled = false; }
});
$('save').disabled = true;
syncView();
loadSample();
