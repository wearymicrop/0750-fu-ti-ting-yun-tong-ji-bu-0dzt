from fastapi import APIRouter, Body, Query
from fastapi.responses import Response

from app.modules.reports.manifest import canonical
from app.modules.revisions import workflow

router = APIRouter(prefix='/api', tags=['修订'])


@router.post('/runs/{report_id}/revisions', summary='在班报上生成修订')
def create(report_id: str, payload: dict = Body(default_factory=dict), dry_run: bool = Query(False)):
    return workflow.create(report_id, payload, formal=not dry_run, persist=not dry_run)


@router.get('/runs/{report_id}/revisions', summary='修订链')
def chain(report_id: str):
    return workflow.lineage(report_id)


@router.get('/runs/{report_id}/export', summary='下载班报证据')
def export(report_id: str):
    bundle = workflow.download(report_id)
    return Response(canonical(bundle) + '\n', media_type='application/json',
                    headers={'Content-Disposition':
                             f'attachment; filename="lift-{bundle["kind"]}-{report_id[:12]}.json"'})
