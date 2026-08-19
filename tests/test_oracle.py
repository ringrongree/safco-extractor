"""Unit tests for the JSON-LD oracle compare helper. Stdlib only (no pytest dep)."""
from __future__ import annotations

import unittest

from app.schemas import Availability, Product
from app.tools.oracle import compare, image_overlap_ratio, normalize_sku


def _p(**kw) -> Product:
    return Product(**kw)


class OracleCompareTests(unittest.TestCase):
    def test_oracle_absent(self):
        report = compare([_p(sku="A", name="n")], None)
        self.assertEqual(report, {"oracle": "absent"})

    def test_row_count_match_headline(self):
        llm = [_p(sku="1025801", name="Foo", price=1.0, currency="USD", availability=Availability.IN_STOCK, description="x")]
        oracle = [_p(sku="1025801", name="Foo", price=1.0, currency="USD", availability=Availability.IN_STOCK, description="y")]
        report = compare(llm, oracle)
        self.assertTrue(report["row_count_match"])
        self.assertTrue(report["field_match"]["1025801"]["name"])
        self.assertTrue(report["field_match"]["1025801"]["description"])  # presence, not text
        self.assertFalse(report["fan_out_diverged"])

    def test_hyphen_vs_bare_sku_is_unmatched(self):
        llm = [_p(sku="102-5801", name="Foo")]
        oracle = [_p(sku="1025801", name="Foo")]
        report = compare(llm, oracle)
        self.assertFalse(report["row_count_match"])
        self.assertEqual(report["llm_only_skus"], ["102-5801"])
        self.assertEqual(report["oracle_only_skus"], ["1025801"])
        self.assertEqual(normalize_sku("102-5801"), "102-5801")

    def test_price_null_null_and_tolerance(self):
        a = [_p(sku="S", price=None)]
        b = [_p(sku="S", price=None)]
        self.assertTrue(compare(a, b)["field_match"]["S"]["price"])
        a = [_p(sku="S", price=1.0005)]
        b = [_p(sku="S", price=1.0)]
        self.assertTrue(compare(a, b)["field_match"]["S"]["price"])
        a = [_p(sku="S", price=1.1)]
        b = [_p(sku="S", price=1.0)]
        self.assertFalse(compare(a, b)["field_match"]["S"]["price"])

    def test_image_overlap_does_not_fail_compare(self):
        llm = [_p(sku="S", image_urls=["https://a/1.jpg", "https://a/2.jpg"])]
        oracle = [_p(sku="S", image_urls=["https://a/1.jpg"])]
        report = compare(llm, oracle)
        self.assertTrue(report["row_count_match"])
        self.assertAlmostEqual(report["image_overlap"], 0.5)
        self.assertEqual(image_overlap_ratio([], []), 1.0)


if __name__ == "__main__":
    unittest.main()
