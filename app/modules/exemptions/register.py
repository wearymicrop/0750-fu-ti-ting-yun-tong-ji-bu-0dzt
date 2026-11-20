from app.contracts import issue, text
from app.reasons import REASONS
from app.times import instant

STATUSES = {'approved', 'pending', 'denied'}


def parse_permit(raw):
    device, permit_id, status = (text(raw, key) for key in ('device', 'id', 'status'))
    reason = raw.get('reason')
    if reason is not None:
        reason = text(raw, 'reason')
        if reason not in REASONS:
            raise ValueError('未知豁免原因')
    start, end = instant(raw.get('start')), instant(raw.get('end'))
    if end <= start or status not in STATUSES:
        raise ValueError('申请时段或状态无效')
    approval = raw.get('approval')
    approval = approval.strip() if isinstance(approval, str) else None
    return {'device': device, 'id': permit_id, 'status': status, 'reason': reason,
            'start': start, 'end': end, 'approval': approval}


def has_approval(permit):
    return permit['status'] != 'approved' or bool(permit['approval'])


def permit_register(rows, devices):
    permits, isolated = [], []
    for line, raw in enumerate(rows, 1):
        try:
            parsed = parse_permit(raw)
        except (AttributeError, KeyError, TypeError, ValueError):
            isolated.append(issue(line, 'INVALID_PERMIT', raw))
            continue
        if parsed['device'] not in devices:
            isolated.append(issue(line, 'UNKNOWN_DEVICE', raw))
            continue
        if not has_approval(parsed):
            isolated.append(issue(line, 'MISSING_APPROVAL', raw))
            continue
        permits.append({**parsed, 'line': line, 'raw': raw})
    return permits, isolated
