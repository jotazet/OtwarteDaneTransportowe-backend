from django.core.management.base import BaseCommand
from pathlib import Path
import json
import re
from data_manager.models import StaticFeedEntry, FeedValidationReport, FeedSubmissionHistory

REPORT_ROOT = Path('uploaded_data')

RE_VALIDATION_DIR = re.compile(r'^validation_report_(\d+)_\d+$')


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


class Command(BaseCommand):
    help = 'Process existing validator report.json files and update DB (attach FeedValidationReport and update submission stage).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually write changes to DB. Default is dry-run.')

    def handle(self, *args, **options):
        apply = options['apply']
        found = 0
        changed = 0
        for static_dir in REPORT_ROOT.rglob('static'):
            # find validation_report_* inside
            for child in static_dir.iterdir():
                if not child.is_dir():
                    continue
                m = RE_VALIDATION_DIR.match(child.name)
                if not m:
                    continue
                found += 1
                entry_id = int(m.group(1))
                report_file = child / 'report.json'
                if not report_file.exists():
                    self.stdout.write(f'No report.json in {child}')
                    continue
                try:
                    data = json.loads(report_file.read_text(encoding='utf-8'))
                except Exception as e:
                    self.stdout.write(f'Failed to read {report_file}: {e}')
                    continue
                e_count, w_count, codes = extract_notice_counts(data)
                self.stdout.write(f'Entry {entry_id}: errors={e_count} warnings={w_count} codes={codes}')
                try:
                    entry = StaticFeedEntry.objects.get(pk=entry_id)
                except StaticFeedEntry.DoesNotExist:
                    self.stdout.write(f'  StaticFeedEntry {entry_id} not found in DB')
                    continue
                # decide action
                need_update_report = (not entry.validation_report_id) or (entry.validation_report and entry.validation_report.report_json != data)
                if need_update_report:
                    self.stdout.write('  Will create/update FeedValidationReport')
                    if apply:
                        if entry.validation_report_id:
                            vr = entry.validation_report
                            vr.report_json = data
                            vr.error_count = e_count
                            vr.warning_count = w_count
                            vr.save()
                        else:
                            vr = FeedValidationReport.objects.create(report_json=data, error_count=e_count, warning_count=w_count)
                            entry.validation_report = vr
                            entry.save(update_fields=['validation_report'])
                        changed += 1
                # update submission stage based on errors
                submission = entry.submission
                latest = submission.history.order_by('-created_at').first()
                latest_event = latest.event_type if latest else None
                if e_count > 0:
                    if latest_event != FeedSubmissionHistory.EVENT_REJECTED:
                        self.stdout.write('  Will mark submission as REJECTED')
                        if apply:
                            FeedSubmissionHistory.objects.create(
                                submission=submission,
                                event_type=FeedSubmissionHistory.EVENT_REJECTED,
                                stage_before=submission.current_stage,
                                stage_after=1,
                                cause=', '.join([f'{k}: {v}' for k,v in sorted(codes.items())]) or f'{e_count} errors',
                                actor=None,
                            )
                            changed += 1
                else:
                    # advance to stage 3 if not already advanced and not rejected
                    if latest_event != FeedSubmissionHistory.EVENT_STAGE_ADVANCED and (latest_event != FeedSubmissionHistory.EVENT_REJECTED):
                        self.stdout.write('  Will advance submission to Stage 3')
                        if apply:
                            FeedSubmissionHistory.objects.create(
                                submission=submission,
                                event_type=FeedSubmissionHistory.EVENT_STAGE_ADVANCED,
                                stage_before=submission.current_stage,
                                stage_after=3,
                                actor=None,
                            )
                            changed += 1
        self.stdout.write(f'Found {found} reports; changed {changed} entries (apply={apply})')
