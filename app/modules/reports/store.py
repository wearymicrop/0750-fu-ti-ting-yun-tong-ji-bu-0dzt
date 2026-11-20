import json
import sqlite3
from contextlib import contextmanager

from app.config import database_path
from app.errors import BusinessError
from app.modules.reports.manifest import canonical

_SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    input_json TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)''',
    '''CREATE TABLE IF NOT EXISTS revisions (
    id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    revision_key TEXT NOT NULL,
    formal INTEGER NOT NULL,
    patch_json TEXT NOT NULL,
    input_json TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)''',
    'CREATE INDEX IF NOT EXISTS revisions_root ON revisions (root_id, formal, generation)',
    'CREATE INDEX IF NOT EXISTS revisions_key ON revisions (root_id, revision_key)',
]


def _tables(connection):
    for statement in _SCHEMA:
        connection.execute(statement)


@contextmanager
def database():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    try:
        with connection:
            _tables(connection)
            yield connection
    finally:
        connection.close()


@contextmanager
def locked():
    """单写事务：读取父级、生成修订与落库在同一条链上完成，避免并发分出两条支链。"""
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15, isolation_level=None)
    try:
        connection.execute('BEGIN IMMEDIATE')
        _tables(connection)
        yield connection
        connection.execute('COMMIT')
    except BaseException:
        try:
            connection.execute('ROLLBACK')
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def save(data, report):
    with database() as connection:
        cursor = connection.execute(
            'INSERT INTO reports (id, input_json, snapshot_json) VALUES (?, ?, ?) ON CONFLICT(id) DO NOTHING',
            (report['id'], canonical(data), canonical(report)),
        )
        row = connection.execute('SELECT snapshot_json FROM reports WHERE id = ?', (report['id'],)).fetchone()
        return {**json.loads(row[0]), 'reused': cursor.rowcount == 0}


def report_row(connection, report_id):
    return connection.execute('SELECT input_json, snapshot_json FROM reports WHERE id = ?', (report_id,)).fetchone()


def revision_row(connection, revision_id):
    return connection.execute(
        'SELECT input_json, patch_json, snapshot_json FROM revisions WHERE id = ?', (revision_id,)
    ).fetchone()


def fetch(report_id):
    with database() as connection:
        return node(connection, report_id)


def node(connection, report_id):
    row = report_row(connection, report_id)
    if row is not None:
        return {'kind': 'report', 'input': json.loads(row[0]), 'patch': None,
                'snapshot': json.loads(row[1])}
    row = revision_row(connection, report_id)
    if row is None:
        raise BusinessError('REPORT_NOT_FOUND', '班报不存在', 404)
    return {'kind': 'revision', 'input': json.loads(row[0]), 'patch': json.loads(row[1]),
            'snapshot': json.loads(row[2])}


def head(connection, root_id):
    """当前正式结论：只认对平并已落库的修订；没有修订时原班报即结论。"""
    return connection.execute(
        'SELECT id, generation, snapshot_json FROM revisions '
        'WHERE root_id = ? AND formal = 1 ORDER BY generation DESC, created_at DESC, rowid DESC LIMIT 1',
        (root_id,),
    ).fetchone()


def find_revision(connection, revision_id):
    return connection.execute(
        'SELECT snapshot_json FROM revisions WHERE id = ?', (revision_id,)
    ).fetchone()


def insert_revision(connection, revision):
    cursor = connection.execute(
        'INSERT INTO revisions (id, root_id, parent_id, generation, revision_key, formal, '
        'patch_json, input_json, snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(id) DO NOTHING',
        (revision['id'], revision['root_id'], revision['parent_id'], revision['generation'],
         revision['id'], int(revision['formal']), canonical(revision['patch']),
         canonical(revision['input']), canonical(revision['snapshot'])),
    )
    row = connection.execute('SELECT snapshot_json FROM revisions WHERE id = ?', (revision['id'],)).fetchone()
    return json.loads(row[0]), cursor.rowcount == 0


def revisions_of(connection, root_id):
    return connection.execute(
        'SELECT id, parent_id, generation, formal, created_at, snapshot_json FROM revisions '
        'WHERE root_id = ? ORDER BY generation, created_at, rowid',
        (root_id,),
    ).fetchall()


def history():
    with database() as connection:
        reports = connection.execute(
            'SELECT snapshot_json, created_at FROM reports'
        ).fetchall()
        revisions = connection.execute(
            'SELECT snapshot_json, created_at FROM revisions'
        ).fetchall()
    runs = []
    for encoded, created in reports:
        report = json.loads(encoded)
        runs.append({'id': report['id'], 'kind': 'report', 'manifest': report['manifest'],
                     'created_at': created, 'status': report['result']['reconcile']['status'],
                     'formal': True, 'parent_id': None, 'generation': 0})
    for encoded, created in revisions:
        revision = json.loads(encoded)
        runs.append({'id': revision['id'], 'kind': 'revision', 'manifest': revision['manifest'],
                     'created_at': created, 'status': revision['result']['reconcile']['status'],
                     'formal': revision['formal'], 'parent_id': revision['parent_id'],
                     'generation': revision['generation']})
    runs.sort(key=lambda row: (row['created_at'], row['generation']), reverse=True)
    return runs[:60]
