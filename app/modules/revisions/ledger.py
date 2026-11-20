"""修订台账：把迟到的报码更正与施工审批叠加到留档班报之上。

原始班报始终只读；修订使用独立编号和独立输入摘要，按父级输入合并后重算。
同一更正按 ``device + id`` 就地替换原行，不追加行，因此重复送入不会叠加时长。
"""

import json
from collections import defaultdict
from time import perf_counter_ns

from app.config import RULE_VERSION
from app.contracts import text
from app.errors import BusinessError
from app.modules.exemptions.register import has_approval, parse_permit
from app.modules.reports.manifest import sha256
from app.pipeline import evaluate
from app.reasons import REASONS
from app.times import instant, local_time, utc_time

BUCKETS = ('available', 'responsible', 'exempt', 'missing')


def _event_patch(row, index):
    try:
        device, event_id, reason = (text(row, key) for key in ('device', 'id', 'reason'))
        if reason not in REASONS:
            raise ValueError('更正原因须为已登记原因码')
        start = instant(row.get('start'))
        value = row.get('end')
        end = instant(value) if value is not None else None
        if end is not None and end <= start:
            raise ValueError('恢复时刻必须晚于报码')
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise BusinessError(
            'INVALID_CORRECTION', f'第 {index} 条更正格式有误', 422,
            {'field': 'corrections', 'index': index, 'reason': str(error)},
        ) from error
    return {'device': device, 'id': event_id, 'start': start, 'end': end, 'reason': reason}


def _permit_patch(row, index):
    try:
        return parse_permit(row)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise BusinessError(
            'INVALID_PERMIT_PATCH', f'第 {index} 条审批格式有误', 422,
            {'field': 'permits', 'index': index, 'reason': str(error)},
        ) from error


def permit_identity(permit):
    return tuple(permit.get(key) for key in
                 ('device', 'id', 'start', 'end', 'reason', 'status', 'approval'))


def _base_events(data):
    lines = defaultdict(list)
    for line, raw in enumerate(data.get('events') or [], 1):
        try:
            lines[text(raw, 'device'), text(raw, 'id')].append(line)
        except (AttributeError, TypeError, ValueError):
            continue
    return lines


def _base_permits(data):
    seen = {}
    for line, raw in enumerate(data.get('permits') or [], 1):
        try:
            seen.setdefault(permit_identity(parse_permit(raw)), line)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return seen


def _registered(data):
    keys = set()
    for raw in data.get('devices') or []:
        try:
            keys.add(text(raw, 'device'))
        except (AttributeError, TypeError, ValueError):
            continue
    return keys


def normalize_patch(parent_input, payload):
    note = payload.get('note')
    if note is not None:
        if not isinstance(note, str) or len(note.strip()) > 500:
            raise BusinessError('INVALID_REVISION', '修订说明须为不超过 500 字的文本')
        note = note.strip() or None
    corrections, permits = payload.get('corrections', []), payload.get('permits', [])
    if not isinstance(corrections, list) or len(corrections) > 1000:
        raise BusinessError('INVALID_REVISION', '更正须为数组且最多 1000 行')
    if not isinstance(permits, list) or len(permits) > 500:
        raise BusinessError('INVALID_REVISION', '审批须为数组且最多 500 行')
    if not corrections and not permits:
        raise BusinessError('EMPTY_REVISION', '修订须至少包含一条更正或一份审批')

    base_lines = _base_events(parent_input)
    registered = _registered(parent_input)
    adopted, seen = [], {}
    for index, row in enumerate(corrections, 1):
        value = _event_patch(row, index)
        key = value['device'], value['id']
        if value['device'] not in registered:
            raise BusinessError(
                'CORRECTION_TARGET_MISSING', f'第 {index} 条更正引用未登记设备', 422,
                {'field': 'corrections', 'index': index, 'device': value['device']},
            )
        lines = base_lines.get(key)
        if not lines:
            raise BusinessError(
                'CORRECTION_TARGET_MISSING', f'第 {index} 条更正找不到原报码', 422,
                {'field': 'corrections', 'index': index, 'device': value['device'], 'id': value['id']},
            )
        value = {**value, 'base_lines': list(lines)}
        if key in seen:
            if seen[key]['value'] != value:
                raise BusinessError(
                    'CORRECTION_CONFLICT', f'第 {index} 条更正与同一报码的重复更正不一致', 422,
                    {'field': 'corrections', 'index': index, 'device': value['device'], 'id': value['id']},
                )
            continue
        seen[key] = {'value': value, 'index': index}
        adopted.append(value)
    adopted.sort(key=lambda row: (row['device'], row['id']))

    existing = _base_permits(parent_input)
    parsed, identities = [], set()
    for index, row in enumerate(permits, 1):
        value = _permit_patch(row, index)
        if value['device'] not in registered:
            raise BusinessError(
                'INVALID_PERMIT_PATCH', f'第 {index} 条审批引用未登记设备', 422,
                {'field': 'permits', 'index': index, 'device': value['device']},
            )
        if not has_approval(value):
            raise BusinessError(
                'INVALID_PERMIT_PATCH', f'第 {index} 条审批缺审批凭据', 422,
                {'field': 'permits', 'index': index, 'device': value['device'], 'id': value['id']},
            )
        identity = permit_identity(value)
        if identity in existing:
            parsed.append({**value, 'line': None, 'duplicate_of': existing[identity], 'source_index': index})
            continue
        if identity in identities:
            parsed.append({**value, 'line': None, 'duplicate_of': 'within_patch', 'source_index': index})
            continue
        identities.add(identity)
        parsed.append({**value, 'line': None, 'duplicate_of': None, 'source_index': index})
    parsed.sort(key=lambda row: (row['device'], row['id'], row['start'], row['end'],
                                 row['reason'] or '', row['status'], row['approval'] or ''))
    next_line = len(parent_input.get('permits') or []) + 1
    for row in parsed:
        if row['duplicate_of'] is None:
            row['line'] = next_line
            next_line += 1
    return {'note': note, 'corrections': adopted, 'permits': parsed}


def merge_input(parent_input, patch):
    data = json.loads(json.dumps(parent_input))
    replaced = {}
    for correction in patch['corrections']:
        for line in correction['base_lines']:
            replaced[line] = correction
    events = []
    for line, raw in enumerate(data.get('events') or [], 1):
        correction = replaced.get(line)
        if correction is None:
            events.append(raw)
            continue
        events.append({
            'device': correction['device'], 'id': correction['id'],
            'start': utc_time(correction['start']),
            'end': utc_time(correction['end']) if correction['end'] is not None else None,
            'reason': correction['reason'],
        })
    data['events'] = events
    permits = list(data.get('permits') or [])
    for permit in sorted((row for row in patch['permits'] if row['line'] is not None),
                         key=lambda row: row['line']):
        permits.append({
            'device': permit['device'], 'id': permit['id'],
            'start': utc_time(permit['start']), 'end': utc_time(permit['end']),
            'reason': permit['reason'], 'status': permit['status'], 'approval': permit['approval'],
        })
    data['permits'] = permits
    return data


def _device_delta(parent, revision):
    return {field: revision[field] - parent[field] for field in ('service',) + BUCKETS + ('difference',)}


def verify_balance(parent_result, revision_result):
    """修订必须与服务窗口对平：营业秒不变、每台设备差额为零、增减相抵。"""
    parent_reconcile = parent_result['reconcile']
    revision_reconcile = revision_result['reconcile']
    parent_devices, revision_devices = parent_reconcile['by_device'], revision_reconcile['by_device']
    problems = []
    for device in sorted(set(parent_devices) | set(revision_devices)):
        before, after = parent_devices.get(device), revision_devices.get(device)
        if before is None or after is None:
            problems.append({'device': device, 'why': 'DEVICE_SET_CHANGED'})
            continue
        if before['service'] != after['service']:
            problems.append({'device': device, 'why': 'SERVICE_CHANGED',
                             'parent': before['service'], 'revision': after['service']})
        delta = _device_delta(before, after)
        if sum(delta[field] for field in BUCKETS):
            problems.append({'device': device, 'why': 'SECONDS_NOT_CONSERVED', 'delta': delta})
        if after['difference']:
            problems.append({'device': device, 'why': 'DEVICE_UNBALANCED', 'difference': after['difference']})
    before, after = parent_reconcile['totals'], revision_reconcile['totals']
    if before['service'] != after['service']:
        problems.append({'device': None, 'why': 'TOTAL_SERVICE_CHANGED'})
    if after['difference']:
        problems.append({'device': None, 'why': 'TOTAL_UNBALANCED', 'difference': after['difference']})
    if sum(after[field] - before[field] for field in BUCKETS):
        problems.append({'device': None, 'why': 'TOTAL_NOT_CONSERVED'})
    return {'balanced': not problems, 'problems': problems}


def _segment_view(row, seconds):
    if row is None:
        return None
    return {'bucket': row['bucket'], 'code': row['code'], 'winner_line': row.get('winner_line'),
            'seconds': seconds}


def segment_changes(parent_result, revision_result):
    grouped = {}
    for key, rows in (('parent', parent_result['segments']), ('revision', revision_result['segments'])):
        by_device = defaultdict(list)
        for row in rows:
            by_device[row['device']].append(row)
        grouped[key] = {device: sorted(items, key=lambda row: row['start'])
                        for device, items in by_device.items()}
    changes = []
    for device in sorted(set(grouped['parent']) | set(grouped['revision'])):
        before, after = grouped['parent'].get(device, []), grouped['revision'].get(device, [])
        bounds = sorted({edge for row in before + after for edge in (row['start'], row['end'])})
        left = right = 0
        for lo, hi in zip(bounds, bounds[1:]):
            while left < len(before) and before[left]['end'] <= lo:
                left += 1
            while right < len(after) and after[right]['end'] <= lo:
                right += 1
            old = before[left] if left < len(before) and before[left]['start'] <= lo < before[left]['end'] else None
            new = after[right] if right < len(after) and after[right]['start'] <= lo < after[right]['end'] else None
            if old is None and new is None:
                continue
            same = (old is not None and new is not None and old['bucket'] == new['bucket']
                    and old['code'] == new['code'] and old.get('winner_line') == new.get('winner_line'))
            if same:
                continue
            changes.append({'device': device, 'start': lo, 'end': hi, 'seconds': hi - lo,
                            'parent': _segment_view(old, hi - lo), 'revision': _segment_view(new, hi - lo)})
    return changes


def _event_view(row):
    if row is None:
        return None
    return {field: row.get(field) for field in ('line', 'device', 'id', 'start', 'end', 'reason', 'state', 'lines')}


def event_changes(parent_result, revision_result):
    before = {row['line']: row for row in parent_result['events']['events']}
    after = {row['line']: row for row in revision_result['events']['events']}
    changes = []
    for line in sorted(set(before) | set(after)):
        old, new = _event_view(before.get(line)), _event_view(after.get(line))
        if old != new:
            changes.append({'line': line, 'device': (new or old)['device'], 'id': (new or old)['id'],
                            'parent': old, 'revision': new})
    return changes


def _correction_rows(result):
    by_line = defaultdict(list)
    for event in result['events']['events']:
        for line in event['lines']:
            by_line[line].append(event)
    return by_line


def adopted_corrections(parent_result, revision_result, patch):
    before_rows = _correction_rows(parent_result)
    after_rows = _correction_rows(revision_result)
    adopted = []
    for correction in patch['corrections']:
        lines = correction['base_lines']
        before, seen = [], set()
        for line in lines:
            for event in before_rows.get(line, []):
                if event['line'] in seen:
                    continue
                seen.add(event['line'])
                before.append(_event_view(event))
        after = next((event for event in after_rows.get(lines[0], [])
                      if set(event['lines']) & set(lines)), None)
        adopted.append({
            'device': correction['device'], 'id': correction['id'], 'base_lines': list(lines),
            'value': {'start': correction['start'], 'end': correction['end'],
                      'reason': correction['reason']},
            'before': before, 'after': _event_view(after),
        })
    return adopted


def adopted_permits(revision_result, patch):
    adopted = []
    for permit in patch['permits']:
        entry = {
            'device': permit['device'], 'id': permit['id'], 'line': permit['line'],
            'duplicate_of': permit['duplicate_of'], 'start': permit['start'], 'end': permit['end'],
            'reason': permit['reason'], 'status': permit['status'], 'approval': permit['approval'],
            'applied_seconds': 0, 'segments': [],
        }
        if permit['line'] is not None:
            for segment in revision_result['segments']:
                if permit['line'] in segment['permit_lines']:
                    entry['segments'].append({'start': segment['start'], 'end': segment['end'],
                                              'seconds': segment['seconds']})
                    entry['applied_seconds'] += segment['seconds']
        adopted.append(entry)
    return adopted


def build_diff(parent_result, revision_result, patch):
    before_totals, after_totals = parent_result['reconcile']['totals'], revision_result['reconcile']['totals']
    parent_devices = parent_result['reconcile']['by_device']
    revision_devices = revision_result['reconcile']['by_device']
    by_device = {}
    for device in sorted(set(parent_devices) | set(revision_devices)):
        before, after = parent_devices.get(device), revision_devices.get(device)
        by_device[device] = {
            'parent': before, 'revision': after,
            'delta': _device_delta(before, after) if before and after else None,
        }
    return {
        'totals': {'parent': before_totals, 'revision': after_totals,
                   'delta': {field: after_totals[field] - before_totals[field]
                             for field in ('service',) + BUCKETS + ('difference',)}},
        'by_device': by_device,
        'segments': segment_changes(parent_result, revision_result),
        'events': event_changes(parent_result, revision_result),
        'adopted': adopted_corrections(parent_result, revision_result, patch),
        'permits': adopted_permits(revision_result, patch),
    }


def semantic_digest(data):
    """按语义比较输入：同一时刻的不同时区写法视为未改动，避免制造空修订。"""
    def moment(value):
        try:
            return utc_time(instant(value))
        except (TypeError, ValueError):
            return value

    view = {}
    for key, value in data.items():
        if key in ('start', 'end', 'cutoff'):
            view[key] = moment(value)
        elif key in ('events', 'permits') and isinstance(value, list):
            rows = []
            for raw in value:
                if isinstance(raw, dict) and 'start' in raw and 'end' in raw:
                    rows.append({**raw, 'start': moment(raw.get('start')), 'end': moment(raw.get('end'))})
                else:
                    rows.append(raw)
            view[key] = rows
        else:
            view[key] = value
    return sha256(view)


def revision_id(parent_id, patch):
    return sha256({'parent': parent_id, 'patch': patch, 'rules': RULE_VERSION})


def build_revision(parent_id, parent_snapshot, parent_input, root_id, generation, payload, formal, patch=None):
    """生成修订；``unchanged`` 表示合并后的输入与父级语义相同，不构成新结论。"""
    patch = normalize_patch(parent_input, payload) if patch is None else patch
    merged = merge_input(parent_input, patch)
    unchanged = semantic_digest(merged) == semantic_digest(parent_input)
    started = perf_counter_ns()
    result = evaluate(merged)
    check = verify_balance(parent_snapshot['result'], result)
    diff = {**build_diff(parent_snapshot['result'], result, patch), 'check': check}
    snapshot = {
        'id': revision_id(parent_id, patch),
        'kind': 'revision',
        'parent_id': parent_id,
        'root_id': root_id,
        'generation': generation + 1,
        'formal': bool(formal),
        'reconciled': check['balanced'],
        'unchanged': unchanged,
        'manifest': {
            'source': 'revision', 'parent_id': parent_id, 'root_id': root_id,
            'generation': generation + 1, 'input_sha256': sha256(merged),
            'patch_sha256': sha256(patch), 'rule_version': RULE_VERSION,
            'digest': sha256(result), 'duration_ms': (perf_counter_ns() - started) // 1_000_000,
            'parameters': result['window'],
        },
        'result': result,
        'diff': diff,
    }
    return {**snapshot, 'snapshot': snapshot, 'patch': patch, 'input': merged}


def local_patch(patch):
    """给页面与下载提供同一份带东八区可读时刻的修订输入。"""
    def event(row):
        return {**row, 'start_local': local_time(row['start']),
                'end_local': local_time(row['end']) if row['end'] is not None else None}

    def permit(row):
        return {**row, 'start_local': local_time(row['start']), 'end_local': local_time(row['end'])}

    return {**patch,
            'corrections': [event(row) for row in patch['corrections']],
            'permits': [permit(row) for row in patch['permits']]}
