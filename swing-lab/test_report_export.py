"""Report formatting and outcome semantics without database credentials."""
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import test_strategy_adjustments
import report_export

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


class ReportExportTests(unittest.TestCase):
    def test_csv_text_cannot_be_interpreted_as_formula_but_negatives_stay_numeric(self):
        for text in ('=1+1', '+SUM(A1)', '-cmd', '@foo', '\t=1', '  =1', '\nhello'):
            self.assertEqual(report_export.cell(text), "'"+text)
        self.assertEqual(report_export.cell(-1.5), -1.5)
        self.assertEqual(report_export.cell(Decimal('-1.00000001')), '-1.00000001')
        self.assertEqual(report_export.cell(None), '')
        self.assertEqual(report_export.cell(False), 'false')

    def test_pending_and_cancelled_are_not_counted_as_filled_losses(self):
        base = {"status": "open", "result_R": 0, "metadata": {"execution": {"pending_entry": True}}}
        row = report_export.outcome(base, NOW)
        self.assertEqual(row['outcome_state'], 'pending_entry')
        self.assertIsNone(row['opportunity_result_R'])
        base.update(status='cancelled', date_closed=NOW.isoformat())
        row = report_export.outcome(base, NOW)
        self.assertEqual(row['opportunity_result_R'], 0)
        self.assertIsNone(row['net_result_R'])

    def test_label_arrival_and_nonfinite_results_remain_explicit(self):
        state = {"status": "closed", "result_R": 1.2, "date_closed": NOW.isoformat()}
        row = report_export.outcome(state, NOW, NOW+timedelta(days=1), True)
        self.assertEqual(row['outcome_state'], 'label_pending')
        self.assertIsNone(row['net_result_R'])
        state['result_R'] = float('nan')
        self.assertEqual(report_export.outcome(state, NOW)['outcome_state'], 'unavailable_or_invalid')

    def test_filter_context_rejects_invalid_dates_and_never_filters_rows(self):
        for start, end in [('2026-02-30', None), ('2026-10-01', '2026-09-01'), ('<script>', None)]:
            with self.assertRaises(ValueError):
                report_export.export_context(start, end)
        self.assertFalse(report_export.export_context('2026-09-01')['applied_to_export_rows'])

    def test_chunk_iterator_closes_temporary_file(self):
        import io
        output = io.BytesIO(b'x'*100000)
        self.assertEqual(len(b''.join(report_export.report_chunks(output))), 100000)
        self.assertTrue(output.closed)
