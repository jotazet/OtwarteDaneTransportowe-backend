#!/usr/bin/env python3
"""
Quick test script: parse a GTFS validator report.json and print error/warning counts
"""
import json
from pathlib import Path

REPORT = Path(__file__).resolve().parents[1] / 'uploaded_data' / '1' / '1' / 'static' / 'validation_report_67_6167' / 'report.json'


def extract_notice_counts(payload: dict):
    summary = payload.get('summary', {}) if isinstance(payload, dict) else {}
    error_fallback = (
        summary.get('errorCount') or summary.get('error_count') or summary.get('errors') or 0
    )
    warning_fallback = (
        summary.get('warningCount') or summary.get('warning_count') or summary.get('warnings') or 0
    )
    candidates = []
    for key in ('notices', 'results', 'noticeResults', 'validationResults'):
        value = payload.get(key)
        if isinstance(value, list):
            candidates.append(value)
    notices = candidates[0] if candidates else []
    error_count = 0
    warning_count = 0
    error_code_counts = {}
    if isinstance(notices, list):
        for notice in notices:
            if not isinstance(notice, dict):
                continue
            severity = str(notice.get('severity', '')).upper()
            total = int(notice.get('totalNotices', 0) or 0)
            code = str(notice.get('code', '')).strip()
            if severity == 'ERROR':
                error_count += max(1, total)
                if code:
                    error_code_counts[code] = error_code_counts.get(code, 0) + max(1, total)
            elif severity == 'WARNING':
                warning_count += max(1, total)
    if error_count == 0 and warning_count == 0:
        error_count = int(error_fallback or 0)
        warning_count = int(warning_fallback or 0)
    return error_count, warning_count, error_code_counts


if __name__ == '__main__':
    if not REPORT.exists():
        print('Report file not found:', REPORT)
        raise SystemExit(2)
    data = json.loads(REPORT.read_text(encoding='utf-8'))
    e, w, codes = extract_notice_counts(data)
    print('Errors:', e)
    print('Warnings:', w)
    print('Codes:', codes)

