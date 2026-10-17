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
    for bucket in data.get('balances', []):
        if bucket.get('user_plan_id') not in active:
            continue
        expires = bucket.get('expires_at') or 0
        if expires and expires <= now:
            continue
        model = registry.resolve(bucket.get('show_name') or '')
        if not registry.knows(model):
            continue
        q = quotas.setdefault(model, {'remaining': 0, 'total': 0, 'used': 0, 'resets_at': 0, 'starts_at': 0})
        q['remaining'] += max(0, bucket.get('available_units', bucket.get('remaining_units', 0)))
        q['total'] += bucket.get('total_units', 0)
        q['used'] += bucket.get('used_units', 0)
        end = bucket.get('period_end') or 0
        if end > now:
            q['resets_at'] = min(q['resets_at'], end) if q['resets_at'] else end
        if bucket.get('entitlement_id'):
            seen_entitlements.add(bucket['entitlement_id'])

    # Plan entitlements with no live balance bucket (e.g. the referral "Global
    # Build" 100M grant is `daily_night` and gets no bucket outside its window).
    # Fold the grant in so the plan's tokens are visible, but record when it
    # becomes effective: a not-yet-open window must NOT be routed to, or the
    # upstream rejects the request ("exceed quota limit").
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
            q = quotas.setdefault(model, {'remaining': 0, 'total': 0, 'used': 0, 'resets_at': 0, 'starts_at': 0})
            q['total'] += grant
            q['remaining'] += grant
            eff = ent.get('effective_at') or 0
            if eff > now:
                # window (e.g. daily_night) not open yet — earliest open start wins
                q['starts_at'] = max(q['starts_at'], eff) if q['starts_at'] else eff
    return plans, quotas
