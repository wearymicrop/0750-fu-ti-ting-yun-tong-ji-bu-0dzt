import json

from app.config import RULE_VERSION
from app.errors import BusinessError
from app.modules.reports.manifest import sha256
from app.modules.reports.store import (
    database, fetch, find_revision, head, insert_revision, locked, node, revisions_of,
)
from app.modules.revisions.ledger import (
    build_revision, local_patch, normalize_patch, revision_id, verify_balance,
)
from app.pipeline import evaluate


def _chain_item(row):
    snapshot = json.loads(row[5])
    return {'id': row[0], 'kind': 'revision', 'parent_id': row[1], 'generation': row[2],
            'formal': bool(row[3]), 'created_at': row[4],
            'digest': snapshot['manifest']['digest'],
            'status': snapshot['result']['reconcile']['status']}


def lineage(node_id):
    """从原班报到指定节点的修订链；链上只有对平并落库的正式结论。"""
    with database() as connection:
        record = node(connection, node_id)
        chain_root = root_id(record, node_id)
        rows = revisions_of(connection, chain_root)
    chain = [{'id': chain_root, 'kind': 'report', 'parent_id': None,
              'generation': 0, 'formal': True, 'created_at': None}]
    chain.extend(_chain_item(row) for row in rows)
    snapshot = record['snapshot']
    return {'root_id': chain_root, 'node_id': node_id,
            'generation': 0 if record['kind'] == 'report' else snapshot['generation'],
            'head_id': chain[-1]['id'], 'chain': chain}


def root_id(record, node_id):
    return node_id if record['kind'] == 'report' else record['snapshot']['root_id']


def resolve_any(node_id):
    with database() as connection:
        return node(connection, node_id)


def detail(node_id):
    """页面读取的一份结论：修订额外带上修订说明与采用的输入。"""
    record = resolve_any(node_id)
    snapshot = record['snapshot']
    if record['kind'] == 'report':
        return snapshot
    return {**snapshot, 'patch': local_patch(record['patch']), 'input': record['input']}


def create(parent_id, payload, formal=True, persist=True):
    """在父级上生成修订。

    父级必须是当前正式结论（否则另行请求会形成分叉），同一请求重复送入返回首次结果。
    只有对平的修订才会落库并成为新的正式结论。
    """
    if not isinstance(payload, dict):
        raise BusinessError('INVALID_REVISION', '修订须为对象')
    with locked() as connection:
        parent = node(connection, parent_id)
        parent_root = root_id(parent, parent_id)
        generation = 0 if parent['kind'] == 'report' else parent['snapshot']['generation']
        patch = normalize_patch(parent['input'], payload)
        # 先按规范化后的修订内容判重：重复送入、换个行序或时区写法、中断后重试都落到同一编号。
        key = revision_id(parent_id, patch)
        stored = find_revision(connection, key)
        if stored is not None:
            return {**json.loads(stored[0]), 'reused': True, 'already_applied': False}
        if persist:
            current = head(connection, parent_root)
            current_id = current[0] if current else parent_root
            if current_id != parent_id:
                raise BusinessError(
                    'STALE_REVISION', '该班报已有更新的正式结论，请基于最新结论修订', 409,
                    {'current_head': current_id},
                )
        revision = build_revision(parent_id, parent['snapshot'], parent['input'],
                                  parent_root, generation, payload, formal, patch=patch)
        body = {field: revision[field] for field in
                ('id', 'kind', 'parent_id', 'root_id', 'generation', 'formal', 'reconciled',
                 'unchanged', 'patch', 'manifest', 'result', 'diff')}
        if not persist:
            return {**body, 'patch': local_patch(revision['patch']), 'input': revision['input'],
                    'reused': False, 'already_applied': revision['unchanged'], 'preview': True}
        if revision['unchanged']:
            return {**parent['snapshot'], 'reused': True, 'already_applied': True}
        if not revision['reconciled']:
            raise BusinessError(
                'REVISION_UNBALANCED', '修订未对平，不能成为正式结论', 409,
                {**revision['diff']['check'], 'revision_id': revision['id']},
            )
        saved, reused = insert_revision(connection, revision)
        return {**saved, 'reused': reused, 'already_applied': False}


def rerun(node_id):
    fetched = fetch(node_id)
    snapshot, data, patch = fetched['snapshot'], fetched['input'], fetched['patch']
    fresh = evaluate(data)
    digest = sha256(fresh)
    if patch is None:
        identity = sha256({'input': sha256(data), 'rules': RULE_VERSION, 'source': snapshot['manifest']['source']})
        if identity != snapshot['id'] or digest != snapshot['manifest']['digest']:
            raise BusinessError('RERUN_MISMATCH', '重算结果与留档不一致', 409)
    else:
        identity = sha256({'parent': snapshot['parent_id'], 'patch': patch, 'rules': RULE_VERSION})
        if identity != snapshot['id'] or digest != snapshot['manifest']['digest']:
            raise BusinessError('RERUN_MISMATCH', '重算结果与留档不一致', 409)
        parent = resolve_any(snapshot['parent_id'])
        check = verify_balance(parent['snapshot']['result'], fresh)
        if not check['balanced']:
            raise BusinessError('REVISION_UNBALANCED', '留档修订已不再对平', 409, check)
    return {**snapshot, 'reused': True}


def download(node_id):
    """下载包与页面读取同源：同一份输入、片段、对账与差异。"""
    fetched = fetch(node_id)
    snapshot, data = fetched['snapshot'], fetched['input']
    if sha256(data) != snapshot['manifest']['input_sha256'] or sha256(snapshot['result']) != snapshot['manifest']['digest']:
        raise BusinessError('EXPORT_MISMATCH', '留档摘要与内容不一致，拒绝导出', 409)
    if fetched['patch'] is None:
        return {'kind': 'report', 'id': snapshot['id'], 'input': data,
                'result': snapshot['result'], 'manifest': snapshot['manifest']}
    if sha256(fetched['patch']) != snapshot['manifest']['patch_sha256']:
        raise BusinessError('EXPORT_MISMATCH', '留档修订摘要与内容不一致，拒绝导出', 409)
    return {
        'kind': 'revision', 'id': snapshot['id'], 'parent_id': snapshot['parent_id'],
        'root_id': snapshot['root_id'], 'generation': snapshot['generation'],
        'formal': snapshot['formal'], 'reconciled': snapshot['reconciled'],
        'patch': local_patch(fetched['patch']), 'input': data,
        'result': snapshot['result'], 'reconcile': snapshot['result']['reconcile'],
        'diff': snapshot['diff'], 'manifest': snapshot['manifest'],
    }
