"""Synthetic identifiers only; no corpus or HTTP requests."""
import unittest
from evidence_reasoning.identifier_filter import identifier_evidence_matches
class IdentifierTests(unittest.TestCase):
    def test_absent_fragment_is_not_evidence(self):
        self.assertFalse(identifier_evidence_matches('KB_ABSENT_A1234567','KB is a generic index heading'))
    def test_exact_identifier_matches(self):
        self.assertTrue(identifier_evidence_matches('REF_A1234567','Excerpt discusses REF_A1234567.'))
    def test_longer_prefix_is_not_the_same_identifier(self):
        self.assertFalse(identifier_evidence_matches('REF_A1234567','REF_A12345678'))
    def test_ordinary_query_unchanged(self):
        self.assertTrue(identifier_evidence_matches('CT MRI comparison','Example prose'))
    def test_one_identifier_does_not_prove_full_coverage(self):
        self.assertTrue(identifier_evidence_matches('REF_A1234567 REF_B7654321','REF_A1234567 only'))
        # This return means candidate relevance; the answer contract must retain
        # missing B as partial coverage. It is not an all-conditions assertion.
if __name__=='__main__':unittest.main()
