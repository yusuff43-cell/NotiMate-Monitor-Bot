"""Real-PostgreSQL checks for «Бухгалтер» (numbering, idempotency, locking).

Skipped unless TEST_DATABASE_URL points at a disposable database, e.g.
  TEST_DATABASE_URL='postgresql:///notimate_test?host=/tmp' python -m unittest test_accountant_postgres
Every test uses its own tenant id and cleans up after itself.
"""

import os
import threading
import unittest
import uuid

URL = os.environ.get('TEST_DATABASE_URL')

FIELDS = {'doc_type': 'tax_invoice', 'seller': 'Makro', 'tax_id': '010', 'doc_ref': 'A1', 'doc_date': '2026-09-10',
          'subtotal': 100, 'vat': 7, 'total': 107, 'currency': 'THB', 'payment_method': 'card', 'confidence': 0.9, 'note': ''}


@unittest.skipUnless(URL, 'TEST_DATABASE_URL not set')
class PostgresDocumentsTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from notimate.packs.accountant.store import PostgresDocumentsStore
        from notimate.projections.operations_store import PostgresOperationsStore
        self.tenant = 'test-' + uuid.uuid4().hex[:8]
        self.store = PostgresDocumentsStore(URL)
        self.store.initialize()
        self.store.initialize()  # idempotent
        PostgresOperationsStore(URL).initialize()
        self.psycopg = psycopg
        self.addCleanup(self.cleanup)

    def cleanup(self):
        with self.psycopg.connect(URL) as conn:
            for table in ('documents', 'document_counters', 'document_questions', 'document_periods', 'operations'):
                conn.execute(f'DELETE FROM {table} WHERE tenant_id = %s', (self.tenant,))

    def new_doc(self, event, sha=None, date='2026-09-10'):
        doc_id, created = self.store.create_document(self.tenant, event, 's1', {**FIELDS, 'doc_date': date}, sha or event, f'{self.tenant}/{event}.jpg', '2026-09-24')
        return doc_id, created

    def test_create_is_idempotent_per_event(self):
        first, created1 = self.new_doc('ev1')
        again, created2 = self.new_doc('ev1')
        self.assertEqual(first, again)
        self.assertEqual((created1, created2), (True, False))

    def test_numbers_are_sequential_per_period_and_gap_free_after_reject(self):
        a, _ = self.new_doc('a')
        b, _ = self.new_doc('b')
        c, _ = self.new_doc('c')
        self.store.reject_document(b)
        self.assertEqual(self.store.confirm_document(a)['doc_number'], '2026-09-001')
        self.assertEqual(self.store.confirm_document(c)['doc_number'], '2026-09-002')
        other_month, _ = self.new_doc('o', date='2026-10-01')
        self.assertEqual(self.store.confirm_document(other_month)['doc_number'], '2026-10-001')

    def test_double_confirm_and_confirm_after_reject_raise(self):
        from notimate.packs.accountant.store import DocumentAlreadyFinalized
        a, _ = self.new_doc('a')
        self.store.confirm_document(a)
        with self.assertRaises(DocumentAlreadyFinalized):
            self.store.confirm_document(a)
        b, _ = self.new_doc('b')
        self.store.reject_document(b)
        with self.assertRaises(DocumentAlreadyFinalized):
            self.store.confirm_document(b)
        with self.assertRaises(KeyError):
            self.store.confirm_document(-1)

    def test_concurrent_confirms_never_share_a_number(self):
        ids = [self.new_doc(f'c{i}')[0] for i in range(8)]
        numbers, errors = [], []

        def confirm(doc_id):
            try:
                numbers.append(self.store.confirm_document(doc_id)['doc_number'])
            except Exception as exc:  # pragma: no cover - would fail the assertions below
                errors.append(exc)

        threads = [threading.Thread(target=confirm, args=(i,)) for i in ids]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(sorted(numbers), [f'2026-09-{n:03d}' for n in range(1, 9)])

    def test_hash_lookup_ignores_rejected_and_lists_confirmed_only(self):
        a, _ = self.new_doc('a', sha='same')
        self.assertEqual(self.store.find_by_hash(self.tenant, 'same')['id'], a)
        self.store.reject_document(a)
        self.assertIsNone(self.store.find_by_hash(self.tenant, 'same'))
        b, _ = self.new_doc('b')
        self.new_doc('c')
        self.store.confirm_document(b)
        self.assertEqual([d['doc_number'] for d in self.store.list_confirmed(self.tenant, '2026-09')], ['2026-09-001'])

    def test_operations_view_questions_and_period(self):
        from notimate.projections.operations_store import PostgresOperationsStore
        PostgresOperationsStore(URL).record_operation(self.tenant, 'k1', 'expense', '2026-09-11', 107, 'THB', 'Makro', 'milk')
        PostgresOperationsStore(URL).record_operation(self.tenant, 'k2', 'expense', '2026-10-01', 5, 'THB', 'X', 'other month')
        ops = self.store.list_operations(self.tenant, '2026-09')
        self.assertEqual([float(o['amount']) for o in ops], [107.0])
        qid = self.store.add_question(self.tenant, 'acc', 'нужен tax invoice', '2026-09', '2026-09-001')
        self.assertTrue(qid)
        self.assertEqual(len(self.store.open_questions(self.tenant, '2026-09')), 1)
        self.assertEqual(len(self.store.open_questions(self.tenant, '2026-08')), 0)
        self.assertIsNone(self.store.period_status(self.tenant, '2026-09'))
        self.store.mark_period(self.tenant, '2026-09', 'sent')
        self.store.mark_period(self.tenant, '2026-09', 'accepted')
        self.assertEqual(self.store.period_status(self.tenant, '2026-09'), 'accepted')


if __name__ == '__main__':
    unittest.main()
