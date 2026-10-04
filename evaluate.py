"""Reproducible control checks; this does not score research or model accuracy."""
import argparse
import io
import json
from pathlib import Path
import unittest

from tests.test_engine import EngineTests

CASES = [
    ('Human review blocks drafting', 'test_review_interrupt_prevents_unapproved_draft'),
    ('SQLite recovery preserves progress', 'test_durable_reopen_resumes_without_repeating_completed_nodes'),
    ('Edited outline reaches approved brief', 'test_edited_outline_survives_approval_and_is_in_brief'),
    ('Rejection prevents a brief', 'test_reject_ends_without_draft'),
    ('Invalid approval remains recoverable', 'test_invalid_resume_does_not_consume_checkpoint'),
    ('Fabricated quotes are refused', 'test_fabricated_quotes_exhaust_budget_without_review'),
    ('Failed fetches stop within budget', 'test_failed_sources_are_retried_once_and_insufficient_evidence_stops'),
    ('Evidence gaps trigger another round', 'test_conditional_second_round_gathers_remaining_sources'),
]


def evaluate():
    results = []
    for label, method in CASES:
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream).run(EngineTests(method))
        entry = {'check': label, 'passed': result.wasSuccessful()}
        if not entry['passed']:
            entry['details'] = stream.getvalue()
        results.append(entry)
    return {'kind': 'deterministic workflow controls', 'passed': sum(item['passed'] for item in results),
            'total': len(results), 'results': results,
            'limitation': 'These checks do not measure model quality, source truth, or semantic claim accuracy.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Optionally save the JSON report')
    args = parser.parse_args()
    report = evaluate()
    print(json.dumps(report, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0 if report['passed'] == report['total'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
