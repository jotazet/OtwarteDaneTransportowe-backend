"""Service alerts (GTFS-RT): OTP's alerts in our shape, with what each one affects.

Texts are the operator's own, in its language; the API passes them on and
codes the rest (effect, cause, severity).
"""

from datetime import datetime

from planner.planning import utc

ALERTS_DOCUMENT = """
query Alerts($feeds: [String!]) {
  alerts(feeds: $feeds) {
    id feed
    alertHeaderText alertDescriptionText alertUrl
    alertEffect alertCause alertSeverityLevel
    activityPeriods { start end }
    entities {
      __typename
      ... on Agency { gtfsId }
      ... on Route { gtfsId }
      ... on Stop { gtfsId }
      ... on Trip { gtfsId route { gtfsId } }
      ... on Pattern { route { gtfsId } }
      ... on StopOnRoute { stop { gtfsId } route { gtfsId } }
      ... on StopOnTrip { stop { gtfsId } trip { gtfsId route { gtfsId } } }
    }
  }
}
"""


def alerts(data: dict, now: datetime) -> list[dict]:
    rows = [_alert(alert, now) for alert in data.get('alerts') or []]
    # Active first, then by ID so pages are stable.
    rows.sort(key=lambda row: (not row['active'], row['id']))
    return rows


def matching(rows, route_ids=None, stop_ids=None, agency_ids=None) -> list[dict]:
    """Alerts about any of the given lines, stops or operators (all when none is given)."""
    if not route_ids and not stop_ids and agency_ids is None:
        return rows
    return [
        row
        for row in rows
        if (route_ids and route_ids & set(row['routes']))
        or (stop_ids and stop_ids & set(row['stops']))
        or (agency_ids and set(agency_ids) & set(row['agencies']))
    ]


def _alert(alert: dict, now: datetime) -> dict:
    affects = {'agencies': set(), 'routes': set(), 'stops': set(), 'trips': set()}
    for entity in alert.get('entities') or []:
        _collect(entity or {}, affects)
    periods = [
        {'start': utc(period.get('start')), 'end': utc(period.get('end'))}
        for period in alert.get('activityPeriods') or []
    ]
    return {
        'id': alert['id'],
        'feed_id': alert.get('feed'),
        'header': alert.get('alertHeaderText'),
        'description': alert.get('alertDescriptionText'),
        'url': alert.get('alertUrl'),
        'effect': _lower(alert.get('alertEffect')),
        'cause': _lower(alert.get('alertCause')),
        'severity': _lower(alert.get('alertSeverityLevel')),
        'active': not periods or any(_covers(period, now) for period in periods),
        'active_periods': periods,
        **{key: sorted(ids) for key, ids in affects.items()},
    }


def _collect(entity: dict, affects: dict) -> None:
    """IDs from one AlertEntity; a trip's alert also counts for its line."""
    kind = entity.get('__typename')
    if kind in ('Agency', 'Route', 'Stop'):
        affects[{'Agency': 'agencies', 'Route': 'routes', 'Stop': 'stops'}[kind]].add(
            entity['gtfsId']
        )
        return
    trip = entity if kind == 'Trip' else entity.get('trip')
    if trip:
        affects['trips'].add(trip['gtfsId'])
    route = (trip or entity).get('route')
    if route:
        affects['routes'].add(route['gtfsId'])
    if entity.get('stop'):
        affects['stops'].add(entity['stop']['gtfsId'])


def _covers(period: dict, now: datetime) -> bool:
    start = period['start'] and datetime.fromisoformat(period['start'])
    end = period['end'] and datetime.fromisoformat(period['end'])
    return (not start or start <= now) and (not end or now <= end)


def _lower(value: str | None) -> str | None:
    return value.lower() if value else None
