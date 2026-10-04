"""DXCC grouping and pinned offline parser checks."""
import os
from pathlib import Path
import unittest

import build_db as b


class DxccRankingTests(unittest.TestCase):
    def row(self, entry_id, score, adif='', source='', inferred='', eligibility='eligible', overlay='R'):
        return dict(entry_id=entry_id, score=str(score), category_source='SOAB (R)',
                    dxcc_adif=adif, dxcc_entity_source=source, dxcc_entity_inferred=inferred,
                    award_eligibility=eligibility, overlay_category_source=overlay)

    def test_source_and_inferred_share_canonical_entity_and_ties(self):
        rows = [self.row('a', 100, '291', 'USA-3'),
                self.row('b', 100, '291', inferred='United States'),
                self.row('c', 90, '291', inferred='United States'),
                self.row('d', 1000, '230', inferred='Germany'),
                self.row('e', 2000, '291', inferred='United States', eligibility='ineligible'),
                self.row('f', 2000)]
        ranks = b._calculate_partition_ranks(rows)
        for scope in ('category_in_dxcc_rank_calculated', 'overlay_in_dxcc_rank_calculated'):
            self.assertEqual([ranks[key][scope] for key in 'abcdef'], [1, 1, 3, 1, None, None])

    def test_unmapped_contest_entities_still_rank(self):
        rows = [self.row('a', 100, source='Custom Source Entity'), self.row('b', 50, source='custom source entity')]
        ranks = b._calculate_partition_ranks(rows)
        self.assertEqual(ranks['a']['category_in_dxcc_rank_calculated'], 1)
        self.assertEqual(ranks['b']['category_in_dxcc_rank_calculated'], 2)


@unittest.skipUnless(os.environ.get('TESTAG_DXCC_REFERENCE_DIR'), 'Set TESTAG_DXCC_REFERENCE_DIR for pinned parser tests')
class HistoricalParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = b.CallsignDxcc(Path(os.environ['TESTAG_DXCC_REFERENCE_DIR']))

    def test_deleted_entity_and_portable_calls(self):
        cases = [('OK1AYY', 1992, 218), ('OK1AYY', 1993, 503), ('BI1XJT', 2025, 318),
                 ('W1AW', 2025, 291), ('DL1ABC/P', 2020, 230), ('KH6/W1AW', 2020, 110)]
        for call, year, adif in cases:
            with self.subTest(call=call, year=year):
                answer = self.parser.infer(call, year)
                self.assertEqual((answer[0], answer[2]), (adif, 'callsign'))

    def test_missing_date_and_ambiguous_prefix_are_not_guessed(self):
        self.assertEqual(self.parser.infer('DL1ABC/MM', 2020)[2], 'no_entity')
        self.assertEqual(self.parser.infer('PJ2ABC', 2010)[2], 'ambiguous_year')
        self.assertEqual(self.parser.infer('3D2ABC', 2020)[2], 'ambiguous_prefix')
        self.assertEqual(self.parser.infer('NOT-A-CALL', 2020)[2], 'unresolved')

    def test_explicit_source_codes_are_normalized_without_station_lookup(self):
        for label, adif in [('USA-3', 291), ('JA', 339), ('Japan', 339), ('318', 318)]:
            self.assertEqual(self.parser.source_adif(label, 2025), adif)
        self.assertIsNone(self.parser.source_adif('unrecognized source', 2025))
