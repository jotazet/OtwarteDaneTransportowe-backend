"""Reading tables straight from GTFS archives, for what the build needs before OTP."""

import csv
import io
import zipfile
from datetime import date

WEEKDAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')


def rows(archive: zipfile.ZipFile, filename: str):
    """The rows of a table as dicts; none if the archive lacks it.

    The table at the root wins; otherwise one in a folder is read.
    """
    names = archive.namelist()
    name = filename if filename in names else None
    name = name or next((n for n in names if n.rsplit('/', 1)[-1] == filename), None)
    if name is None:
        return
    with archive.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding='utf-8-sig', errors='replace'))


def service_period(path: str) -> tuple[date, date] | None:
    """First and last day of a GTFS file's service, or None if it cannot be read.

    From calendar.txt rows that run on some weekday and the days calendar_dates.txt
    adds. A row's start_date may precede its first running weekday by a few days.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            days = [
                _field(row, column)
                for row in rows(archive, 'calendar.txt')
                if any(_field(row, day) == '1' for day in WEEKDAYS)
                for column in ('start_date', 'end_date')
            ] + [
                _field(row, 'date')
                for row in rows(archive, 'calendar_dates.txt')
                if _field(row, 'exception_type') == '1'
            ]
        if not days:
            return None
        # YYYYMMDD strings sort like the dates they stand for, and Python 3.11+
        # reads them as basic ISO dates.
        return date.fromisoformat(min(days)), date.fromisoformat(max(days))
    except (OSError, zipfile.BadZipFile, ValueError, csv.Error):
        return None


def _field(row: dict, name: str) -> str:
    # A short row has None for its missing fields.
    return (row.get(name) or '').strip()
