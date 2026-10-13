"""The planner's street map: the OSM extract, cut down to where the included feeds stop.

A whole-Poland street graph does not fit the production VPS (docs/deployment.md,
sizing), so only ~3 km grid cells around stops are kept, plus one cell of
margin, and only the ways OTP routes on (the tag filter from OTP's docs).
"""

import csv
import hashlib
import io
import json
import os
import subprocess
import zipfile

# About 3 x 3 km in Poland.
CELL_LAT = 0.027
CELL_LON = 0.045

TAG_FILTER = [
    'w/highway',
    'wa/public_transport=platform',
    'wa/railway=platform',
    'w/park_ride=yes',
    'r/type=restriction',
    'r/type=route',
]
OSMIUM_TIMEOUT_SECONDS = 1800
# Reading a header takes well under a second, even for all of Poland.
HEADER_TIMEOUT_SECONDS = 60


class OsmPreparationError(Exception):
    pass


def pbf_header(path: str) -> dict:
    """osmium's reading of a PBF file's header: format, bounding boxes, data timestamp.

    Raises OsmPreparationError for anything that is not an OSM PBF extract.
    """
    try:
        result = subprocess.run(
            # The format explicitly: osmium guesses it from the name, and a
            # download in progress ends in .download.
            ['osmium', 'fileinfo', '--json', '--input-format', 'pbf', path],
            capture_output=True,
            text=True,
            timeout=HEADER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OsmPreparationError(str(exc)) from exc
    if result.returncode != 0:
        raise OsmPreparationError(result.stderr[-2000:])
    info = json.loads(result.stdout)
    if info.get('file', {}).get('format') != 'PBF' or not info.get('header', {}).get('boxes'):
        raise OsmPreparationError('Not an OSM PBF extract with a bounding box.')
    return info


def stop_cells(feed_paths) -> set[tuple[int, int]]:
    """Grid cells holding a stop of any of the GTFS files, plus their neighbours."""
    cells = set()
    for path in feed_paths:
        with zipfile.ZipFile(path) as archive:
            name = next(
                (n for n in archive.namelist() if n.rsplit('/', 1)[-1] == 'stops.txt'), None
            )
            if name is None:
                continue
            with archive.open(name) as raw:
                text = io.TextIOWrapper(raw, encoding='utf-8-sig', errors='replace')
                for row in csv.DictReader(text):
                    try:
                        lat, lon = float(row['stop_lat']), float(row['stop_lon'])
                    except (KeyError, TypeError, ValueError):
                        continue
                    i, j = int(lat // CELL_LAT), int(lon // CELL_LON)
                    cells.update((i + di, j + dj) for di in (-1, 0, 1) for dj in (-1, 0, 1))
    return cells


def street_graph_key(cells, osm_path: str, otp_image: str) -> str:
    """Changes whenever the street graph has to be rebuilt."""
    stat = os.stat(osm_path)
    digest = hashlib.sha256()
    digest.update(f'{otp_image}|{stat.st_size}|{stat.st_mtime_ns}|'.encode())
    digest.update(json.dumps(sorted(cells)).encode())
    return digest.hexdigest()


def prepare_osm(source: str, cells, target: str) -> None:
    """Write ``source`` filtered to routable ways and cropped to the cells to ``target``."""
    workdir = os.path.dirname(target)
    polygons = [
        [
            [
                [j * CELL_LON, i * CELL_LAT],
                [(j + 1) * CELL_LON, i * CELL_LAT],
                [(j + 1) * CELL_LON, (i + 1) * CELL_LAT],
                [j * CELL_LON, (i + 1) * CELL_LAT],
                [j * CELL_LON, i * CELL_LAT],
            ]
        ]
        for i, j in sorted(cells)
    ]
    coverage = os.path.join(workdir, 'coverage.geojson')
    with open(coverage, 'w') as fh:
        json.dump(
            {
                'type': 'Feature',
                'properties': {},
                'geometry': {'type': 'MultiPolygon', 'coordinates': polygons},
            },
            fh,
        )
    filtered = os.path.join(workdir, 'filtered.osm.pbf')
    try:
        _osmium('tags-filter', source, *TAG_FILTER, '-o', filtered)
        _osmium(
            'extract', '--strategy', 'complete_ways', '--polygon', coverage, filtered, '-o', target
        )
    finally:
        for path in (coverage, filtered):
            if os.path.exists(path):
                os.remove(path)


def _osmium(*args):
    try:
        subprocess.run(
            ['osmium', *args, '--overwrite', '-f', 'pbf,add_metadata=false'],
            check=True,
            capture_output=True,
            timeout=OSMIUM_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OsmPreparationError(str(exc)) from exc
