from fastapi import APIRouter, Body

from app.errors import BusinessError
from app.modules.reports.evidence import segment_evidence
from app.modules.reports.manifest import build_report
from app.modules.reports.store import fetch, history, save
from app.modules.revisions.workflow import detail, rerun
from app.pipeline import evaluate
from app.samples import load_sample

router = APIRouter(prefix='/api', tags=['班报归档'])


@router.get('/preview/{sample}', summary='预览样例')
def preview(sample: str):
    return evaluate(load_sample(sample))


@router.get('/preview/{sample}/evidence', summary='复核样例片段')
def preview_evidence(sample: str, device: str, start: int):
    return segment_evidence(evaluate(load_sample(sample)), device, start)


@router.post('/runs', summary='保存班报')
def create(payload: dict = Body(default_factory=dict)):
    sample, data = payload.get('sample'), payload.get('data')
    if isinstance(sample, str) and 'data' not in payload:
        return save_run(sample, load_sample(sample))
    if isinstance(data, dict) and 'sample' not in payload:
        return save_run('custom', data)
    raise BusinessError('CHOOSE_SOURCE', '请选择样例或提交一份班报数据')


def save_run(source, data):
    try:
        report = build_report(source, data)
    except (ValueError, OverflowError, RecursionError) as error:
        raise BusinessError('INVALID_DATA', '输入包含无法归档的值') from error
    return save(data, report)


@router.get('/runs', summary='最近班报')
def list_reports():
    return {'runs': history()}


@router.get('/runs/{report_id}', summary='读取班报或修订')
def read(report_id: str):
    return detail(report_id)


@router.get('/runs/{report_id}/evidence', summary='复核归档片段')
def report_evidence(report_id: str, device: str, start: int):
    return segment_evidence(fetch(report_id)['snapshot']['result'], device, start)


@router.post('/runs/{report_id}/rerun', summary='按留档输入重跑')
def rerun_run(report_id: str):
    return rerun(report_id)
