"""Bike and scooter sharing (GBFS) on the map: OTP's rental stations and free vehicles."""

RENTALS_DOCUMENT = """
query Rentals(
  $minLat: CoordinateValue!, $minLon: CoordinateValue!,
  $maxLat: CoordinateValue!, $maxLon: CoordinateValue!
) {
  vehicleRentalsByBbox(
    minimumLatitude: $minLat, minimumLongitude: $minLon,
    maximumLatitude: $maxLat, maximumLongitude: $maxLon
  ) {
    __typename
    ... on VehicleRentalStation {
      stationId name lat lon allowPickupNow allowDropoffNow realtime
      rentalNetwork { networkId }
      availableVehicles { total byType { count vehicleType { formFactor propulsionType } } }
      availableSpaces { total }
    }
    ... on RentalVehicle {
      vehicleId name lat lon allowPickupNow
      rentalNetwork { networkId }
      vehicleType { formFactor propulsionType }
      fuel { percent range }
    }
  }
}
"""


# OTP reports a station without a dock limit as Java's Integer.MAX_VALUE.
NO_LIMIT = 2**31 - 1


def bbox_variables(bbox) -> dict:
    min_lon, min_lat, max_lon, max_lat = bbox
    return {'minLat': min_lat, 'minLon': min_lon, 'maxLat': max_lat, 'maxLon': max_lon}


def rentals(data: dict) -> list[dict]:
    rows = [
        _station(place) if place['__typename'] == 'VehicleRentalStation' else _vehicle(place)
        for place in data.get('vehicleRentalsByBbox') or []
        if place
    ]
    rows.sort(key=lambda row: (row['kind'], row['id'] or ''))
    return rows


# GBFS 2.3 replaced `scooter` with these two; asking for scooters means all of them.
FORM_FACTOR_VARIANTS = {'scooter': {'scooter', 'scooter_standing', 'scooter_seated'}}


def matching(rows, networks=None, form_factors=None) -> list[dict]:
    if networks:
        rows = [row for row in rows if row['network'] in networks]
    if form_factors:
        wanted = set().union(*(FORM_FACTOR_VARIANTS.get(f, {f}) for f in form_factors))
        rows = [
            row for row in rows if {kind['form_factor'] for kind in row['vehicle_types']} & wanted
        ]
    return rows


def _station(station: dict) -> dict:
    available = station.get('availableVehicles') or {}
    return {
        'kind': 'station',
        'id': station['stationId'],
        'network': (station.get('rentalNetwork') or {}).get('networkId'),
        'name': station.get('name'),
        'lat': station['lat'],
        'lon': station['lon'],
        'vehicles_available': available.get('total'),
        'spaces_available': _limit((station.get('availableSpaces') or {}).get('total')),
        'vehicle_types': [
            {**_vehicle_type(kind.get('vehicleType')), 'count': kind.get('count')}
            for kind in available.get('byType') or []
        ],
        'pickup_now': station.get('allowPickupNow'),
        'dropoff_now': station.get('allowDropoffNow'),
        'battery_percent': None,
        'range_m': None,
    }


def _vehicle(vehicle: dict) -> dict:
    fuel = vehicle.get('fuel') or {}
    percent = fuel.get('percent')
    return {
        'kind': 'vehicle',
        'id': vehicle['vehicleId'],
        'network': (vehicle.get('rentalNetwork') or {}).get('networkId'),
        'name': vehicle.get('name'),
        'lat': vehicle['lat'],
        'lon': vehicle['lon'],
        'vehicles_available': None,
        'spaces_available': None,
        'vehicle_types': [{**_vehicle_type(vehicle.get('vehicleType')), 'count': 1}],
        'pickup_now': vehicle.get('allowPickupNow'),
        'dropoff_now': None,
        # GBFS gives a fraction (0.29); people read percent.
        'battery_percent': round(percent * 100) if percent is not None else None,
        'range_m': fuel.get('range'),
    }


def _limit(value: int | None) -> int | None:
    return None if value is None or value >= NO_LIMIT else value


def _vehicle_type(kind: dict | None) -> dict:
    kind = kind or {}
    return {
        'form_factor': (kind.get('formFactor') or '').lower() or None,
        'propulsion': (kind.get('propulsionType') or '').lower() or None,
    }
