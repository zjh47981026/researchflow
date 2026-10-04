"""Offline tests exercise the real graph and SQLite checkpoint store."""
import tempfile
from pathlib import Path
import unittest
from uuid import uuid4

from pydantic import ValidationError

from researchflow.engine import Engine, MAX_ROUNDS, verify_claim
from researchflow.models import Claim, EvidenceBatch, Plan, ReviewDecision

QUESTION = 'How do the selected frameworks support durable research?'
URLS = [f'https://example.com/source-{number}' for number in range(1, 5)]
QUOTE = 'Durable checkpoints preserve workflow state for later review.'


def source(url, source_id):
    return {'id': source_id, 'url': url, 'title': f'Fixture {source_id}',
            'text': f'{QUOTE} Each source is a controlled offline test fixture.', 'error': ''}


class Provider:
    def __init__(self, fabricate=False):
        self.plan_calls = 0
        self.extract_calls = 0
        self.fabricate = fabricate

    def plan(self, question):
        self.plan_calls += 1
        return Plan(outline=['Durable state', 'Human review'])

    def extract(self, question, sources, outline):
        self.extract_calls += 1
        return EvidenceBatch(claims=[Claim(claim=QUOTE, source_id=item['id'],
                                           quote='An invented quotation never present in any fixture.' if self.fabricate else QUOTE)
                                     for item in sources if not item.get('error')])


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'checkpoints.sqlite'
        self.provider = Provider()
        self.engine = Engine(self.path, fetcher=source, researcher=self.provider)

    def tearDown(self):
        self.engine.close()
        self.directory.cleanup()

    def start(self, urls=None):
        return self.engine.start(QUESTION, urls or URLS[:2])

    def test_review_interrupt_prevents_unapproved_draft(self):
        identifier = self.start()
        state = self.engine.snapshot(identifier)
        self.assertEqual(state['status'], 'awaiting_review')
        self.assertEqual(state['brief'], '')
        self.assertEqual(state['rounds'], 1)
        self.assertEqual([item['stage'] for item in state['events']], ['plan', 'gather', 'verify'])
        self.assertEqual({item['source_id'] for item in state['claims']}, {'S1', 'S2'})

    def test_durable_reopen_resumes_without_repeating_completed_nodes(self):
        identifier = self.start()
        self.engine.close()
        self.engine = Engine(self.path, fetcher=source, researcher=self.provider)
        self.assertEqual(self.engine.snapshot(identifier)['status'], 'awaiting_review')
        result = self.engine.resume(identifier, {'approved': True})
        self.assertEqual(result['status'], 'complete')
        self.assertIn('[S1]', result['brief'])
        self.assertEqual((self.provider.plan_calls, self.provider.extract_calls), (1, 1))

    def test_edited_outline_survives_approval_and_is_in_brief(self):
        identifier = self.start()
        headings = ['Evidence overview', 'Checkpoint recovery']
        result = self.engine.resume(identifier, {'approved': True, 'outline': headings})
        self.assertEqual(result['outline'], headings)
        self.assertIn('- Evidence overview', result['brief'])
        self.assertEqual(result['status'], 'complete')

    def test_reject_ends_without_draft(self):
        identifier = self.start()
        result = self.engine.resume(identifier, {'approved': False})
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['brief'], '')
        self.assertNotIn('draft', [item['stage'] for item in result['events']])

    def test_invalid_resume_does_not_consume_checkpoint(self):
        identifier = self.start()
        for invalid in ({'approved': 'true'}, {'approved': True, 'outline': ['Only one']},
                        {'approved': True, 'outline': ['Good', 'Bad\nheading']},
                        {'approved': True, 'unexpected': 'value'}):
            with self.assertRaises(ValidationError):
                self.engine.resume(identifier, invalid)
            self.assertEqual(self.engine.snapshot(identifier)['status'], 'awaiting_review')
        self.assertEqual(self.engine.resume(identifier, {'approved': True})['status'], 'complete')

    def test_completed_run_cannot_be_resumed_again(self):
        identifier = self.start()
        self.engine.resume(identifier, {'approved': True})
        with self.assertRaises(ValueError):
            self.engine.resume(identifier, {'approved': True})
        self.assertEqual(len(self.engine.snapshot(identifier)['events']), 5)

    def test_fabricated_quotes_exhaust_budget_without_review(self):
        self.engine.researcher = Provider(fabricate=True)
        identifier = self.start()
        state = self.engine.snapshot(identifier)
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['rounds'], MAX_ROUNDS)
        self.assertEqual(state['claims'], [])
        self.assertEqual(state['brief'], '')
        self.assertTrue(any('Rejected quote' in error for error in state['errors']))
        self.assertEqual(self.engine.researcher.extract_calls, MAX_ROUNDS)

    def test_redirect_aliases_do_not_count_as_distinct_sources(self):
        self.engine.fetcher = lambda url, identifier: {**source(url, identifier), 'url': 'https://example.com/canonical'}
        state = self.engine.snapshot(self.start())
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['rounds'], MAX_ROUNDS)
        self.assertEqual({item['source_id'] for item in state['claims']}, {'S1', 'S2'})
        self.assertEqual(state['brief'], '')

    def test_recovery_continues_saved_node_without_replanning(self):
        identifier = str(uuid4())
        initial = {'question': QUESTION, 'urls': URLS[:2], 'mode': 'demo', 'status': 'planning',
                   'sources': [], 'claims': [], 'candidates': [], 'rounds': 0,
                   'events': [], 'errors': [], 'brief': '', 'outline': []}
        self.engine.graph.invoke(initial, self.engine._config(identifier), interrupt_before=['gather'])
        self.assertEqual(self.provider.plan_calls, 1)
        self.assertEqual(self.provider.extract_calls, 0)
        self.engine.close()
        self.engine = Engine(self.path, fetcher=source, researcher=self.provider)
        recovered = self.engine.recover(identifier)
        self.assertEqual(recovered['status'], 'awaiting_review')
        self.assertEqual((self.provider.plan_calls, self.provider.extract_calls), (1, 1))
        self.assertEqual(self.engine.recover(identifier)['status'], 'awaiting_review')

    def test_failed_sources_are_retried_once_and_insufficient_evidence_stops(self):
        calls = []
        def failing(url, source_id):
            calls.append(source_id)
            return {**source(url, source_id), 'text': '', 'error': 'fixture unavailable'}
        self.engine.fetcher = failing
        identifier = self.start()
        state = self.engine.snapshot(identifier)
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(calls, ['S1', 'S2', 'S1', 'S2'])
        self.assertEqual(state['rounds'], 2)
        self.assertTrue(any('Insufficient evidence' in error for error in state['errors']))

    def test_conditional_second_round_gathers_remaining_sources(self):
        calls = []
        def selective(url, source_id):
            calls.append(source_id)
            item = source(url, source_id)
            return {**item, 'text': '', 'error': 'fixture unavailable'} if source_id in ('S2', 'S3') else item
        self.engine.fetcher = selective
        identifier = self.start(URLS)
        state = self.engine.snapshot(identifier)
        self.assertEqual(state['status'], 'awaiting_review')
        self.assertEqual(state['rounds'], 2)
        self.assertEqual(calls, ['S1', 'S2', 'S3', 'S2', 'S3', 'S4'])
        self.assertEqual({item['source_id'] for item in state['claims']}, {'S1', 'S4'})
        self.assertEqual(len(state['claims']), 2, 'Repeated candidates should be deduplicated')

    def test_planning_failure_stops_before_source_requests(self):
        class BrokenProvider(Provider):
            def plan(self, question):
                raise RuntimeError('fixture model failure')
        calls = []
        self.engine.researcher = BrokenProvider()
        self.engine.fetcher = lambda url, identifier: calls.append(identifier)
        state = self.engine.snapshot(self.start())
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['rounds'], 0)
        self.assertEqual(calls, [])

    def test_run_ids_are_unique_and_unknown_ids_fail(self):
        identifier = self.start()
        with self.assertRaises(ValueError):
            self.engine.start(QUESTION, URLS[:2], thread_id=identifier)
        with self.assertRaises(KeyError):
            self.engine.snapshot(str(uuid4()))
        with self.assertRaises(ValueError):
            self.engine.snapshot('../checkpoints')

    def test_input_rejects_duplicate_urls_and_unsupported_mode(self):
        for question, urls, mode in [('short', URLS[:2], 'demo'), (QUESTION, [URLS[0]] * 2, 'demo'),
                                     (QUESTION, URLS[:2], 'unknown'), (QUESTION, ['http://example.com/a', URLS[1]], 'live')]:
            with self.assertRaises(ValueError):
                self.engine.start(question, urls, mode=mode)

    def test_model_extraction_failure_is_bounded(self):
        class BrokenProvider(Provider):
            def extract(self, question, sources, outline):
                self.extract_calls += 1
                raise RuntimeError('fixture timeout')
        self.engine.researcher = BrokenProvider()
        state = self.engine.snapshot(self.start())
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(self.engine.researcher.extract_calls, MAX_ROUNDS)
        self.assertEqual(state['brief'], '')


class EvidenceTests(unittest.TestCase):
    def test_quote_whitespace_normalization_preserves_provenance_check(self):
        claim = {'claim': QUOTE, 'source_id': 'S1', 'quote': 'Durable checkpoints\n preserve workflow state for later review.'}
        valid, error = verify_claim(claim, [source(URLS[0], 'S1')])
        self.assertIsNotNone(valid)
        self.assertIsNone(error)

    def test_unknown_and_failed_source_are_rejected(self):
        claim = {'claim': QUOTE, 'source_id': 'S1', 'quote': QUOTE}
        for sources in ([], [{**source(URLS[0], 'S1'), 'error': 'unavailable'}]):
            valid, error = verify_claim(claim, sources)
            self.assertIsNone(valid)
            self.assertIn('unavailable source', error)

    def test_invalid_short_or_nonexistent_quote_is_rejected(self):
        for quote in ('too short', 'An invented quotation never present in this source.'):
            valid, error = verify_claim({'claim': QUOTE, 'source_id': 'S1', 'quote': quote}, [source(URLS[0], 'S1')])
            self.assertIsNone(valid)
            self.assertIsNotNone(error)

    def test_schema_limits_prevent_unbounded_model_output(self):
        for invalid in ({'outline': ['One']}, {'outline': ['A', 'B'], 'extra': True},
                        {'outline': ['X' * 101, 'B']}):
            with self.assertRaises(ValidationError):
                Plan.model_validate(invalid)
        claim = {'claim': QUOTE, 'source_id': 'S1', 'quote': QUOTE}
        with self.assertRaises(ValidationError):
            EvidenceBatch.model_validate({'claims': [claim] * 11})
        with self.assertRaises(ValidationError):
            ReviewDecision.model_validate({'approved': 1})


if __name__ == '__main__':
    unittest.main()
"""Offline tests exercise the real graph and SQLite checkpoint store."""
import tempfile
from pathlib import Path
import unittest
from uuid import uuid4

from pydantic import ValidationError

from researchflow.engine import Engine, MAX_ROUNDS, verify_claim
from researchflow.models import Claim, EvidenceBatch, Plan, ReviewDecision

QUESTION = 'How do the selected frameworks support durable research?'
URLS = [f'https://example.com/source-{number}' for number in range(1, 5)]
QUOTE = 'Durable checkpoints preserve workflow state for later review.'


def source(url, source_id):
    return {'id': source_id, 'url': url, 'title': f'Fixture {source_id}',
            'text': f'{QUOTE} Each source is a controlled offline test fixture.', 'error': ''}


class Provider:
    def __init__(self, fabricate=False):
        self.plan_calls = 0
        self.extract_calls = 0
        self.fabricate = fabricate

    def plan(self, question):
        self.plan_calls += 1
        return Plan(outline=['Durable state', 'Human review'])

    def extract(self, question, sources, outline):
        self.extract_calls += 1
        return EvidenceBatch(claims=[Claim(claim=QUOTE, source_id=item['id'],
                                           quote='An invented quotation never present in any fixture.' if self.fabricate else QUOTE)
                                     for item in sources if not item.get('error')])


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'checkpoints.sqlite'
        self.provider = Provider()
        self.engine = Engine(self.path, fetcher=source, researcher=self.provider)

    def tearDown(self):
        self.engine.close()
        self.directory.cleanup()

    def start(self, urls=None):
        return self.engine.start(QUESTION, urls or URLS[:2])

    def test_review_interrupt_prevents_unapproved_draft(self):
        identifier = self.start()
        state = self.engine.snapshot(identifier)
        self.assertEqual(state['status'], 'awaiting_review')
        self.assertEqual(state['brief'], '')
        self.assertEqual(state['rounds'], 1)
        self.assertEqual([item['stage'] for item in state['events']], ['plan', 'gather', 'verify'])
        self.assertEqual({item['source_id'] for item in state['claims']}, {'S1', 'S2'})

    def test_durable_reopen_resumes_without_repeating_completed_nodes(self):
        identifier = self.start()
        self.engine.close()
        self.engine = Engine(self.path, fetcher=source, researcher=self.provider)
        self.assertEqual(self.engine.snapshot(identifier)['status'], 'awaiting_review')
        result = self.engine.resume(identifier, {'approved': True})
        self.assertEqual(result['status'], 'complete')
        self.assertIn('[S1]', result['brief'])
        self.assertEqual((self.provider.plan_calls, self.provider.extract_calls), (1, 1))

    def test_edited_outline_survives_approval_and_is_in_brief(self):
        identifier = self.start()
        headings = ['Evidence overview', 'Checkpoint recovery']
        result = self.engine.resume(identifier, {'approved': True, 'outline': headings})
        self.assertEqual(result['outline'], headings)
        self.assertIn('- Evidence overview', result['brief'])
        self.assertEqual(result['status'], 'complete')

    def test_reject_ends_without_draft(self):
        identifier = self.start()
        result = self.engine.resume(identifier, {'approved': False})
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['brief'], '')
        self.assertNotIn('draft', [item['stage'] for item in result['events']])

    def test_invalid_resume_does_not_consume_checkpoint(self):
        identifier = self.start()
        for invalid in ({'approved': 'true'}, {'approved': True, 'outline': ['Only one']},
                        {'approved': True, 'outline': ['Good', 'Bad\nheading']},
                        {'approved': True, 'unexpected': 'value'}):
            with self.assertRaises(ValidationError):
                self.engine.resume(identifier, invalid)
            self.assertEqual(self.engine.snapshot(identifier)['status'], 'awaiting_review')
        self.assertEqual(self.engine.resume(identifier, {'approved': True})['status'], 'complete')

    def test_completed_run_cannot_be_resumed_again(self):
        identifier = self.start()
        self.engine.resume(identifier, {'approved': True})
        with self.assertRaises(ValueError):
            self.engine.resume(identifier, {'approved': True})
        self.assertEqual(len(self.engine.snapshot(identifier)['events']), 5)

    def test_fabricated_quotes_exhaust_budget_without_review(self):
        self.engine.researcher = Provider(fabricate=True)
        identifier = self.start()
        state = self.engine.snapshot(identifier)
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['rounds'], MAX_ROUNDS)
        self.assertEqual(state['claims'], [])
        self.assertEqual(state['brief'], '')
        self.assertTrue(any('Rejected quote' in error for error in state['errors']))
        self.assertEqual(self.engine.researcher.extract_calls, MAX_ROUNDS)

    def test_redirect_aliases_do_not_count_as_distinct_sources(self):
        self.engine.fetcher = lambda url, identifier: {**source(url, identifier), 'url': 'https://example.com/canonical'}
        state = self.engine.snapshot(self.start())
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['rounds'], MAX_ROUNDS)
        self.assertEqual({item['source_id'] for item in state['claims']}, {'S1', 'S2'})
        self.assertEqual(state['brief'], '')

    def test_recovery_continues_saved_node_without_replanning(self):
        identifier = str(uuid4())
        initial = {'question': QUESTION, 'urls': URLS[:2], 'mode': 'demo', 'status': 'planning',
                   'sources': [], 'claims': [], 'candidates': [], 'rounds': 0,
                   'events': [], 'errors': [], 'brief': '', 'outline': []}
        self.engine.graph.invoke(initial, self.engine._config(identifier), interrupt_before=['gather'])
        self.assertEqual(self.provider.plan_calls, 1)
        self.assertEqual(self.provider.extract_calls, 0)
        self.engine.close()
        self.engine = Engine(self.path, fetcher=source, researcher=self.provider)
        recovered = self.engine.recover(identifier)
        self.assertEqual(recovered['status'], 'awaiting_review')
        self.assertEqual((self.provider.plan_calls, self.provider.extract_calls), (1, 1))
        self.assertEqual(self.engine.recover(identifier)['status'], 'awaiting_review')

    def test_failed_sources_are_retried_once_and_insufficient_evidence_stops(self):
        calls = []
        def failing(url, source_id):
            calls.append(source_id)
            return {**source(url, source_id), 'text': '', 'error': 'fixture unavailable'}
        self.engine.fetcher = failing
        identifier = self.start()
        state = self.engine.snapshot(identifier)
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(calls, ['S1', 'S2', 'S1', 'S2'])
        self.assertEqual(state['rounds'], 2)
        self.assertTrue(any('Insufficient evidence' in error for error in state['errors']))

    def test_conditional_second_round_gathers_remaining_sources(self):
        calls = []
        def selective(url, source_id):
            calls.append(source_id)
            item = source(url, source_id)
            return {**item, 'text': '', 'error': 'fixture unavailable'} if source_id in ('S2', 'S3') else item
        self.engine.fetcher = selective
        identifier = self.start(URLS)
        state = self.engine.snapshot(identifier)
        self.assertEqual(state['status'], 'awaiting_review')
        self.assertEqual(state['rounds'], 2)
        self.assertEqual(calls, ['S1', 'S2', 'S3', 'S2', 'S3', 'S4'])
        self.assertEqual({item['source_id'] for item in state['claims']}, {'S1', 'S4'})
        self.assertEqual(len(state['claims']), 2, 'Repeated candidates should be deduplicated')

    def test_planning_failure_stops_before_source_requests(self):
        class BrokenProvider(Provider):
            def plan(self, question):
                raise RuntimeError('fixture model failure')
        calls = []
        self.engine.researcher = BrokenProvider()
        self.engine.fetcher = lambda url, identifier: calls.append(identifier)
        state = self.engine.snapshot(self.start())
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['rounds'], 0)
        self.assertEqual(calls, [])

    def test_run_ids_are_unique_and_unknown_ids_fail(self):
        identifier = self.start()
        with self.assertRaises(ValueError):
            self.engine.start(QUESTION, URLS[:2], thread_id=identifier)
        with self.assertRaises(KeyError):
            self.engine.snapshot(str(uuid4()))
        with self.assertRaises(ValueError):
            self.engine.snapshot('../checkpoints')

    def test_input_rejects_duplicate_urls_and_unsupported_mode(self):
        for question, urls, mode in [('short', URLS[:2], 'demo'), (QUESTION, [URLS[0]] * 2, 'demo'),
                                     (QUESTION, URLS[:2], 'unknown'), (QUESTION, ['http://example.com/a', URLS[1]], 'live')]:
            with self.assertRaises(ValueError):
                self.engine.start(question, urls, mode=mode)

    def test_model_extraction_failure_is_bounded(self):
        class BrokenProvider(Provider):
            def extract(self, question, sources, outline):
                self.extract_calls += 1
                raise RuntimeError('fixture timeout')
        self.engine.researcher = BrokenProvider()
        state = self.engine.snapshot(self.start())
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(self.engine.researcher.extract_calls, MAX_ROUNDS)
        self.assertEqual(state['brief'], '')


class EvidenceTests(unittest.TestCase):
    def test_quote_whitespace_normalization_preserves_provenance_check(self):
        claim = {'claim': QUOTE, 'source_id': 'S1', 'quote': 'Durable checkpoints\n preserve workflow state for later review.'}
        valid, error = verify_claim(claim, [source(URLS[0], 'S1')])
        self.assertIsNotNone(valid)
        self.assertIsNone(error)

    def test_unknown_and_failed_source_are_rejected(self):
        claim = {'claim': QUOTE, 'source_id': 'S1', 'quote': QUOTE}
        for sources in ([], [{**source(URLS[0], 'S1'), 'error': 'unavailable'}]):
            valid, error = verify_claim(claim, sources)
            self.assertIsNone(valid)
            self.assertIn('unavailable source', error)

    def test_invalid_short_or_nonexistent_quote_is_rejected(self):
        for quote in ('too short', 'An invented quotation never present in this source.'):
            valid, error = verify_claim({'claim': QUOTE, 'source_id': 'S1', 'quote': quote}, [source(URLS[0], 'S1')])
            self.assertIsNone(valid)
            self.assertIsNotNone(error)

    def test_schema_limits_prevent_unbounded_model_output(self):
        for invalid in ({'outline': ['One']}, {'outline': ['A', 'B'], 'extra': True},
                        {'outline': ['X' * 101, 'B']}):
            with self.assertRaises(ValidationError):
                Plan.model_validate(invalid)
        claim = {'claim': QUOTE, 'source_id': 'S1', 'quote': QUOTE}
        with self.assertRaises(ValidationError):
            EvidenceBatch.model_validate({'claims': [claim] * 11})
        with self.assertRaises(ValidationError):
            ReviewDecision.model_validate({'approved': 1})


if __name__ == '__main__':
    unittest.main()
