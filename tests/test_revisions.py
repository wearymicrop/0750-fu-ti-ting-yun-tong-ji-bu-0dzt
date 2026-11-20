from concurrent.futures import ThreadPoolExecutor

from app.modules.reports.manifest import sha256
from app.samples import load_sample


def archive(client, sample='clean'):
    return client.post('/api/runs', json={'sample': sample}).json()


def revise(client, report_id, body, **params):
    return client.post(f'/api/runs/{report_id}/revisions', json=body, params=params)


def correction(**overrides):
    row = {
        'device': '002', 'id': 'E-201', 'start': '2026-11-20T00:00:00+08:00',
        'end': '2026-11-20T00:40:00+08:00', 'reason': 'POWER',
    }
    return {**row, **overrides}


def permit(**overrides):
    row = {
        'device': '001', 'id': 'P-900', 'start': '2026-11-19T22:30:00+08:00',
        'end': '2026-11-19T22:45:00+08:00', 'reason': 'SAFETY', 'status': 'approved',
        'approval': '补批施工单 31',
    }
    return {**row, **overrides}


def test_original_report_stays_unchanged_after_revision(client):
    original = archive(client)
    body = {'corrections': [correction(device='002', id='E-201', reason='POWER')], 'note': '报码更正'}
    revised = revise(client, original['id'], body).json()
    again = client.get(f"/api/runs/{original['id']}").json()
    assert again['result'] == original['result'] and again['manifest'] == original['manifest']
    assert revised['kind'] == 'revision' and revised['parent_id'] == original['id']
    assert revised['generation'] == 1 and revised['formal'] is True and revised['reconciled'] is True
    assert revised['root_id'] == original['id'] and revised['id'] != original['id']


def test_adopting_a_corrected_code_replaces_the_line_without_adding_seconds(client):
    original = archive(client)
    before = original['result']['reconcile']['totals']
    revised = revise(client, original['id'], {'corrections': [correction()]}).json()
    after = revised['result']['reconcile']['totals']
    assert revised['result']['reconcile']['status'] == 'MATCHED'
    assert after['service'] == before['service']
    assert sum(after[key] - before[key] for key in ('available', 'responsible', 'exempt', 'missing')) == 0
    assert revised['diff']['adopted'][0]['value']['reason'] == 'POWER'
    assert revised['diff']['by_device']['001']['delta']['service'] == 0


def test_duplicate_identical_correction_does_not_stack(client):
    original = archive(client)
    body = {'corrections': [correction(device='002', id='E-201', reason='POWER')]}
    first = revise(client, original['id'], body).json()
    second = revise(client, original['id'], body).json()
    assert second['id'] == first['id'] and second['reused'] is True
    rows = client.get(f"/api/runs/{first['id']}").json()['result']['events']['events']
    assert len([row for row in rows if row['id'] == 'E-201']) == 1
    assert first['result']['reconcile']['totals'] == second['result']['reconcile']['totals']


def test_repeating_the_same_correction_on_the_new_head_does_not_stack(client):
    original = archive(client)
    body = {'corrections': [correction(device='002', id='E-201', reason='POWER')]}
    first = revise(client, original['id'], body).json()
    replay = revise(client, first['id'], {'corrections': [correction(device='002', id='E-201')]}).json()
    assert replay['already_applied'] is True and replay['id'] == first['id']
    assert replay['result'] == first['result']


def test_correction_must_point_at_an_existing_code(client):
    original = archive(client)
    response = revise(client, original['id'], {'corrections': [correction(id='E-999')]})
    assert response.status_code == 422 and response.json()['code'] == 'CORRECTION_TARGET_MISSING'


def test_contradictory_duplicate_corrections_are_rejected(client):
    original = archive(client)
    body = {'corrections': [correction(device='002', id='E-201'), correction(device='002', id='E-201', reason='SAFETY')]}
    response = revise(client, original['id'], body)
    assert response.status_code == 422 and response.json()['code'] == 'CORRECTION_CONFLICT'


def test_late_permit_turns_responsible_time_into_exempt(client):
    original = archive(client)
    revised = revise(client, original['id'], {'permits': [permit(start='2026-11-19T22:15:00+08:00', end='2026-11-19T22:30:00+08:00')]}).json()
    before, after = original['result']['reconcile']['totals'], revised['result']['reconcile']['totals']
    assert after['exempt'] == before['exempt'] + 900
    assert after['responsible'] == before['responsible'] - 900
    assert revised['diff']['permits'][0]['applied_seconds'] == 900
    assert revised['diff']['permits'][0]['line'] == 2


def test_permit_overlap_with_existing_approval_counts_once(client):
    original = archive(client)
    body = {'permits': [permit(start='2026-11-19T22:00:00+08:00', end='2026-11-19T23:00:00+08:00')]}
    revised = revise(client, original['id'], body).json()
    totals = revised['result']['reconcile']['totals']
    assert totals['exempt'] == 3600
    assert totals['exempt'] == sum(row['seconds'] for row in revised['result']['segments']
                                   if row['bucket'] == 'exempt')


def test_same_permit_already_in_the_base_is_not_added_twice(client):
    original = archive(client)
    existing = load_sample('clean')['permits'][0]
    revised = revise(client, original['id'], {'permits': [existing]}).json()
    assert revised['already_applied'] is True and revised['id'] == original['id']
    assert revised['result']['reconcile']['totals'] == original['result']['reconcile']['totals']


def test_unbalanced_revision_never_becomes_formal(client, monkeypatch):
    from app.modules.revisions import ledger

    original = archive(client)
    real = ledger.verify_balance

    def broken(parent_result, revision_result):
        check = real(parent_result, revision_result)
        return {'balanced': False, 'problems': check['problems'] + [{'device': '001', 'why': 'DEVICE_UNBALANCED'}]}

    monkeypatch.setattr(ledger, 'verify_balance', broken)
    response = revise(client, original['id'], {'corrections': [correction()]})
    assert response.status_code == 409 and response.json()['code'] == 'REVISION_UNBALANCED'
    chain = client.get(f"/api/runs/{original['id']}/revisions").json()['chain']
    assert [row['id'] for row in chain] == [original['id']]
    assert client.get(f"/api/runs/{original['id']}").json()['result'] == original['result']


def test_balance_verification_rejects_service_or_conservation_breaks():
    from app.modules.revisions.ledger import verify_balance

    def totals(service, available, responsible, exempt, missing):
        return {'service': service, 'available': available, 'responsible': responsible,
                'exempt': exempt, 'missing': missing,
                'difference': service - available - responsible - exempt - missing}

    def result(service, available, responsible, exempt, missing, device='001'):
        row = totals(service, available, responsible, exempt, missing)
        return {'reconcile': {'totals': row, 'by_device': {device: dict(row)}}}

    parent = result(100, 40, 30, 20, 10)
    assert verify_balance(parent, result(100, 30, 40, 20, 10))['balanced'] is True
    assert verify_balance(parent, result(100, 40, 30, 20, 20))['balanced'] is False
    longer = verify_balance(parent, result(130, 50, 40, 20, 10))['problems']
    assert {'SERVICE_CHANGED', 'TOTAL_SERVICE_CHANGED', 'DEVICE_UNBALANCED'} <= {row['why'] for row in longer}
    assert all(row['why'] != 'DEVICE_SET_CHANGED' for row in longer)
    missing_device = verify_balance(parent, result(100, 40, 30, 20, 10, device='002'))
    assert {row['why'] for row in missing_device['problems']} == {'DEVICE_SET_CHANGED'}


def test_revision_chain_lists_original_and_formal_revisions(client):
    original = archive(client)
    first = revise(client, original['id'], {'corrections': [correction(device='002', id='E-201', reason='POWER')]}).json()
    second = revise(client, first['id'], {'permits': [permit(device='002', id='P-900', reason='MECHANICAL', start='2026-11-20T00:00:00+08:00', end='2026-11-20T00:10:00+08:00')]}).json()
    chain = client.get(f"/api/runs/{second['id']}/revisions").json()
    assert [row['id'] for row in chain['chain']] == [original['id'], first['id'], second['id']]
    assert chain['head_id'] == second['id'] and chain['generation'] == 2


def test_revision_on_stale_parent_is_rejected(client):
    original = archive(client)
    first = revise(client, original['id'], {'corrections': [correction(device='002', id='E-201', reason='POWER')]}).json()
    assert first['generation'] == 1
    response = revise(client, original['id'], {'permits': [permit()]})
    assert response.status_code == 409 and response.json()['code'] == 'STALE_REVISION'


def test_dry_run_previews_without_becoming_head(client):
    original = archive(client)
    preview = revise(client, original['id'], {'corrections': [correction()]}, dry_run='true').json()
    assert preview['preview'] is True and preview['formal'] is False
    assert client.get(f"/api/runs/{original['id']}/revisions").json()['chain'] == [
        {'id': original['id'], 'kind': 'report', 'parent_id': None, 'generation': 0,
         'formal': True, 'created_at': None},
    ]
    formal = revise(client, original['id'], {'corrections': [correction()]}).json()
    assert formal['generation'] == 1 and formal['id'] == preview['id']


def test_running_two_requests_concurrently_creates_one_formal_revision(client):
    original = archive(client)
    body = {'corrections': [correction()]}
    with ThreadPoolExecutor(max_workers=4) as executor:
        copies = list(executor.map(lambda _: revise(client, original['id'], body).json(), range(4)))
    assert len({row['id'] for row in copies}) == 1
    chain = client.get(f"/api/runs/{original['id']}/revisions").json()['chain']
    assert len(chain) == 2


def test_revision_can_be_rerun_from_archive(client):
    original = archive(client)
    revised = revise(client, original['id'], {'corrections': [correction(device='002', id='E-201', reason='POWER')]}).json()
    again = client.post(f"/api/runs/{revised['id']}/rerun").json()
    assert again['id'] == revised['id'] and again['manifest']['digest'] == revised['manifest']['digest']
    original_again = client.post(f"/api/runs/{original['id']}/rerun").json()
    assert original_again['id'] == original['id']


def test_export_matches_the_page_and_adopted_inputs(client):
    original = archive(client)
    revised = revise(client, original['id'], {'corrections': [correction()], 'permits': [permit()]}).json()
    page = client.get(f"/api/runs/{revised['id']}").json()
    bundle = client.get(f"/api/runs/{revised['id']}/export").json()
    assert bundle['kind'] == 'revision' and bundle['id'] == revised['id']
    assert bundle['result'] == page['result'] and bundle['diff'] == page['diff']
    assert sha256(bundle['input']) == revised['manifest']['input_sha256']
    assert sha256(bundle['result']) == revised['manifest']['digest']
    assert [row['value']['reason'] for row in bundle['diff']['adopted']] == ['POWER']
    assert bundle['diff']['permits'][0]['approval'] == '补批施工单 31'


def test_export_of_original_matches_archive(client):
    original = archive(client)
    bundle = client.get(f"/api/runs/{original['id']}/export").json()
    assert bundle['kind'] == 'report' and bundle['result'] == original['result']
    assert sha256(bundle['input']) == original['manifest']['input_sha256']
    assert sha256(bundle['result']) == original['manifest']['digest']


def test_evidence_lookup_works_on_revision(client):
    original = archive(client)
    revised = revise(client, original['id'], {'permits': [permit()]}).json()
    row = next(row for row in revised['result']['segments'] if row['bucket'] == 'exempt')
    evidence = client.get(f"/api/runs/{revised['id']}/evidence",
                          params={'device': '001', 'start': row['start']}).json()
    assert evidence['segment']['bucket'] == 'exempt'
    assert [item['id'] for item in evidence['permits']] == ['P-101', 'P-900']


def test_retry_after_interruption_does_not_stack_seconds(client):
    original = archive(client)
    body = {'corrections': [correction()], 'permits': [permit()]}
    first = revise(client, original['id'], body).json()
    # 模拟请求已落库但响应丢失后重试：即使行序或时区写法变化，也返回同一份正式结论。
    reordered = {
        'permits': [{**permit(), 'start': '2026-11-19T14:30:00Z', 'end': '2026-11-19T14:45:00Z'}],
        'corrections': [{**correction(), 'start': '2026-11-19T16:00:00Z', 'end': '2026-11-19T16:40:00Z'}],
    }
    retried = revise(client, original['id'], reordered).json()
    assert retried['id'] == first['id'] and retried['reused'] is True
    stored = client.get(f"/api/runs/{first['id']}").json()
    assert stored['result']['reconcile']['totals'] == first['result']['reconcile']['totals']
    chain = client.get(f"/api/runs/{original['id']}/revisions").json()['chain']
    assert len(chain) == 2


def test_dry_run_then_save_reuses_the_same_revision_id(client):
    original = archive(client)
    body = {'corrections': [correction()]}
    preview = revise(client, original['id'], body, dry_run='true').json()
    saved = revise(client, original['id'], body).json()
    assert saved['id'] == preview['id'] and saved['reused'] is False
    assert saved['result'] == preview['result']


def test_empty_or_unchanged_revision_reports_nothing_new(client):
    original = archive(client)
    empty = revise(client, original['id'], {})
    assert empty.status_code == 422 and empty.json()['code'] == 'EMPTY_REVISION'
    unchanged = revise(client, original['id'], {'corrections': [
        {'device': '001', 'id': 'E-101', 'start': '2026-11-19T22:00:00+08:00',
         'end': '2026-11-19T23:00:00+08:00', 'reason': 'SAFETY'},
    ]}).json()
    assert unchanged['already_applied'] is True and unchanged['reused'] is True
    assert unchanged['id'] == original['id']


def test_revision_cannot_be_grafted_onto_a_missing_report(client):
    response = revise(client, 'missing', {'corrections': [correction()]})
    assert response.status_code == 404 and response.json()['code'] == 'REPORT_NOT_FOUND'
