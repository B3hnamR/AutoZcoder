"""Normalize coding-plan quota buckets; keep resets and plan ends independent."""
from .registry import Registry


def parse_balance(data: dict, now: float):
    registry = Registry()
    plans = [{k: p.get(k) for k in ('user_plan_id', 'plan_id', 'name', 'status', 'starts_at', 'ends_at')}
             for p in data.get('plans', [])]
    active = {p['user_plan_id'] for p in plans if p['status'] == 'active'
              and (not p['starts_at'] or p['starts_at'] <= now)
              and (not p['ends_at'] or p['ends_at'] > now)}
    quotas = {}
    seen_entitlements = set()
    def add(model, remaining, total, used, starts=0, end=0):
        q = quotas.setdefault(model, {'remaining': 0, 'total': 0, 'used': 0,
                                      'resets_at': 0, 'starts_at': 0})
        if starts > now:
            q['pending_remaining'] = q.get('pending_remaining', 0) + remaining
            q['pending_total'] = q.get('pending_total', 0) + total
            q['pending_starts_at'] = min(q.get('pending_starts_at', starts), starts)
        else:
            q['remaining'] += remaining
            q['total'] += total
            q['used'] += used
            if end > now:
                q['resets_at'] = min(q['resets_at'], end) if q['resets_at'] else end

    for bucket in data.get('balances', []):
        if bucket.get('user_plan_id') not in active:
            continue
        if bucket.get('entitlement_id'):
            seen_entitlements.add(bucket['entitlement_id'])
        expires = bucket.get('expires_at') or 0
        if (expires and expires <= now) or (bucket.get('period_end') and bucket['period_end'] <= now):
            continue
        model = registry.resolve(bucket.get('show_name') or '')
        if not registry.knows(model):
            continue
        add(model, max(0, bucket.get('available_units', bucket.get('remaining_units', 0))),
            bucket.get('total_units', 0), bucket.get('used_units', 0),
            max(bucket.get('starts_at') or 0, bucket.get('effective_at') or 0,
                bucket.get('period_start') or 0), bucket.get('period_end') or 0)

    # Plan entitlements with no live balance bucket (e.g. the referral "Global
    # Build" 100M grant is `daily_night` and gets no bucket outside its window).
    # Pending grants are visible separately, never added to live capacity or
    # allowed to close a model that also has a currently usable balance.
    for p in data.get('plans', []):
        if p.get('user_plan_id') not in active:
            continue
        for ent in (p.get('entitlements') or []):
            if not isinstance(ent, dict):
                continue
            if ent.get('entitlement_id') and ent['entitlement_id'] in seen_entitlements:
                continue
            grant = ent.get('grant_units') or 0
            if grant <= 0:
                continue
            model = registry.resolve(ent.get('show_name') or '')
            if not registry.knows(model):
                continue
            add(model, grant, grant, 0, ent.get('effective_at') or 0)
    for q in quotas.values():
        if not q['total'] and not q['remaining'] and not q['used']:
            q['starts_at'] = q.get('pending_starts_at', 0)
    return plans, quotas
