import unittest
from unittest.mock import patch

from stzb_warroom import node_selection as nodes


class SelectionTests(unittest.TestCase):
    def test_fastest_ready_and_no_candidates_ready(self):
        rows = [{"host": host} for host in ("a.example", "b.example", "c.example")]
        with patch.object(nodes, "latency", side_effect=lambda row: {
                "a.example": None, "b.example": 0.2, "c.example": 0.1}[row["host"]]):
            self.assertEqual(nodes.choose(rows), (0.1, "c.example"))
            with self.assertRaises(ValueError):
                nodes.choose(rows[:1])
