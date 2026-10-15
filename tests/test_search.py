import unittest

from cirana.core.search import score, search

APPS = [
    ("1", "Visual Studio Code", "Development"),
    ("2", "Google Chrome", "Browsers"),
    ("3", "Discord", "Communication"),
    ("4", "Character Map", "Utilities"),
]


class SearchTests(unittest.TestCase):
    def test_score_ordering(self):
        self.assertGreater(score("dis", "Discord"), score("cord", "Discord"))
        self.assertGreater(score("studio", "Visual Studio Code"), score("tud", "Visual Studio Code"))
        self.assertEqual(score("zzz", "Discord"), 0)
        self.assertEqual(score("", "Discord"), 0)

    def test_prefix_beats_substring(self):
        results = search("ch", APPS, [], [], [])
        self.assertEqual(results[0].title, "Character Map")

    def test_subsequence_needs_three_letters(self):
        self.assertEqual(score("vc", "Visual Studio Code"), 0)
        self.assertGreater(score("vsc", "Visual Studio Code"), 0)

    def test_empty_query_lists_recents_in_order(self):
        results = search("", APPS, [], [], ["3", "1"])
        self.assertEqual([r.key for r in results], ["3", "1"])

    def test_recent_results_float_up(self):
        base = [r.key for r in search("o", APPS, [], [], [])]
        boosted = [r.key for r in search("o", APPS, [], [], ["3"])]
        self.assertLessEqual(boosted.index("3"), base.index("3"))

    def test_categories_and_workspaces(self):
        results = search("dev", APPS, [("development", "Development", 3), ("empty", "Developer", 0)],
                         [("w1", "Dev setup", 2)], [])
        kinds = {(r.kind, r.key) for r in results}
        self.assertIn(("category", "development"), kinds)
        self.assertIn(("workspace", "w1"), kinds)
        self.assertNotIn(("category", "empty"), kinds)  # empty categories are noise


if __name__ == "__main__":
    unittest.main()
