"""Idle age for dismissible stall alerts.

The board already marks a transcript ``stalled`` after ``--stale`` seconds.
This module answers a different question: which idle, stalled or errored
members have been quiet long enough to bother the operator. The default is
10 minutes. Running and completed members are never included.

``team-board/index.html`` repeats ``member_idle_age`` and ``stall_candidates``
for snapshots that do not carry a ``stallAlerts`` block (the ``?demo=1``
overlay). Keep the two copies in step.
"""

DEFAULT_STALL_ALERT_SEC = 600
MIN_STALL_ALERT_SEC = 60
MAX_STALL_ALERT_SEC = 24 * 60 * 60
ALERT_STATUSES = ('idle', 'error', 'stalled')


def _finite(value):
    """A real number, or None. Booleans are rejected (they are ints)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float('inf'), float('-inf')):
        return None
    return float(value)


def _clip(value, limit):
    if not isinstance(value, str):
        return ''
    return value[:limit]


def normalize_threshold(value, default=DEFAULT_STALL_ALERT_SEC):
    """Clamp to whole seconds. Missing or invalid values use the default."""
    number = _finite(value)
    if number is None:
        number = float(default)
    if number < MIN_STALL_ALERT_SEC:
        number = float(MIN_STALL_ALERT_SEC)
    if number > MAX_STALL_ALERT_SEC:
        number = float(MAX_STALL_ALERT_SEC)
    return int(number)


def member_idle_age(member, now):
    """Return ``(seconds, basis, anchor)``.

    Prefer an explicit non-negative ``idleSeconds``. Otherwise use
    ``lastActivityAt``. If that is missing, use the newest ``timeline``
    ``ts`` or ``endedAt`` — the shortest idle age the snapshot can support.
    ``startedAt`` alone is not an event, so a member with no later timestamp
    stays unknown and is not alerted. Unknown is ``(None, None, None)``.
    """
    now_n = _finite(now)
    if now_n is None or not isinstance(member, dict):
        return None, None, None
    explicit = _finite(member.get('idleSeconds'))
    if explicit is not None and explicit >= 0:
        return explicit, 'idleSeconds', now_n - explicit
    last = _finite(member.get('lastActivityAt'))
    if last is not None and last > 0:
        if last > now_n:
            return 0.0, 'lastActivityAt', last
        return now_n - last, 'lastActivityAt', last
    newest = None
    timeline = member.get('timeline')
    if isinstance(timeline, list):
        for event in timeline:
            if not isinstance(event, dict):
                continue
            ts = _finite(event.get('ts'))
            if ts is not None and ts > 0 and (newest is None or ts > newest):
                newest = ts
    ended = _finite(member.get('endedAt'))
    if ended is not None and ended > 0 and (newest is None or ended > newest):
        newest = ended
    if newest is None:
        return None, None, None
    if newest > now_n:
        return 0.0, 'lastEvent', newest
    return now_n - newest, 'lastEvent', newest


def stall_candidates(members, now):
    """Idle, errored and stalled members with a known age, longest first.

    Ages under the threshold are still returned. The caller decides what is
    active. Members we cannot time are omitted.
    """
    if not isinstance(members, list):
        return []
    found = []
    for member in members:
        if not isinstance(member, dict):
            continue
        status = member.get('status')
        if status not in ALERT_STATUSES:
            continue
        seconds, basis, anchor = member_idle_age(member, now)
        if seconds is None:
            continue
        ident = member.get('id')
        if not isinstance(ident, str):
            ident = ''
        name = _clip(member.get('name'), 80) or ident or '成员'
        found.append({
            'id': ident,
            'name': name,
            'status': status,
            'idleSeconds': int(seconds),
            'basis': basis,
            'anchor': anchor,
            'errorReason': _clip(member.get('errorReason'), 120),
        })
    found.sort(key=lambda item: (-item['idleSeconds'], item['id'], item['status']))
    return found


def active_stall_alerts(candidates, threshold_sec):
    """Candidates at or beyond the threshold, already sorted."""
    threshold = normalize_threshold(threshold_sec)
    if not isinstance(candidates, list):
        return []
    return [item for item in candidates if isinstance(item, dict) and _finite(item.get('idleSeconds')) is not None and item['idleSeconds'] >= threshold]


def attach_stall_alerts(snapshot, now=None, threshold_sec=DEFAULT_STALL_ALERT_SEC):
    """Add ``stallAlerts`` to a snapshot dict. Does not copy the snapshot."""
    if not isinstance(snapshot, dict):
        return snapshot
    if now is None:
        now = snapshot.get('generatedAt')
    threshold = normalize_threshold(threshold_sec)
    snapshot['stallAlerts'] = {
        'thresholdSec': threshold,
        'defaultThresholdSec': DEFAULT_STALL_ALERT_SEC,
        'candidates': stall_candidates(snapshot.get('members'), now),
    }
    return snapshot
