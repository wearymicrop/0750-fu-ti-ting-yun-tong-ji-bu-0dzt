from app.times import instant


def test_full_sample_workflow(client):
    home = client.get('/')
    assert home.status_code == 200 and home.text.count('data-section=') == 7
    assert client.get('/healthz').json()['service'] == 'lift-duties'
    for stage, field in [('schedules', 'devices'), ('events', 'counts'), ('clipping', 'pieces'),
                         ('attribution', 'segments'), ('exemptions', 'permits'), ('reconcile', 'totals')]:
        response = client.get(f'/api/{stage}/dirty')
        assert response.status_code == 200 and field in response.json()
    instant_start = instant('2026-11-19T22:30:00+08:00')
    data = client.get('/api/preview/dirty/evidence', params={'device': '001', 'start': instant_start}).json()
    assert data['segment']['bucket'] == 'exempt' and [row['id'] for row in data['permits']] == ['P-101', 'P-102']


def test_bad_sample_and_empty_input(client):
    sample = client.get('/api/reconcile/no-such-sample')
    assert sample.status_code == 404 and sample.json()['code'] == 'SAMPLE_NOT_FOUND'
    run = client.post('/api/runs', json={})
    assert run.status_code == 422 and run.json()['code'] == 'CHOOSE_SOURCE'


def test_archive_page_offers_revision_review(client):
    home = client.get('/')
    for anchor in ('revision-form-panel', 'correction-rows', 'permit-rows', 'adopted-codes',
                   'adopted-permits', 'revision-devices', 'revision-diff', 'revision-chain'):
        assert f'id="{anchor}"' in home.text
    assert '修订本班报' in home.text and '试算核对' in home.text


def test_revision_from_original_report_reaches_formal_conclusion(client):
    original = client.post('/api/runs', json={'sample': 'clean'}).json()
    assert client.get(f"/api/runs/{original['id']}/revisions").json()['chain'][0]['id'] == original['id']
    response = client.post(f"/api/runs/{original['id']}/revisions", json={
        'note': '交班后补录',
        'corrections': [{'device': '002', 'id': 'E-201', 'start': '2026-11-20T00:00:00+08:00',
                         'end': '2026-11-20T00:40:00+08:00', 'reason': 'POWER'}],
        'permits': [{'device': '001', 'id': 'P-900', 'start': '2026-11-19T22:15:00+08:00',
                     'end': '2026-11-19T22:30:00+08:00', 'reason': 'SAFETY', 'status': 'approved',
                     'approval': '补批施工单 31'}],
    })
    revised = response.json()
    assert response.status_code == 200 and revised['formal'] is True and revised['reconciled'] is True
    assert revised['result']['reconcile']['status'] == 'MATCHED'
    page = client.get(f"/api/runs/{revised['id']}").json()
    assert page['patch']['note'] == '交班后补录'
    assert page['diff']['totals']['delta']['service'] == 0
    assert client.get(f"/api/runs/{revised['id']}/export").json()['diff'] == page['diff']
    assert client.get(f"/api/runs/{original['id']}").json()['result'] == original['result']
