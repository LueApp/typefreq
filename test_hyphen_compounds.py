import unittest

from typefreq.spellcheck import SpellNotifier


class HyphenCompoundTests(unittest.TestCase):
    def test_hyphenated_compounds_are_not_flagged_from_partial_suggestions(self):
        spell = SpellNotifier()

        self.assertEqual(spell.check("end-a"), (False, None))
        self.assertEqual(spell.check("agent-ws"), (False, None))

    def test_unhyphenated_typos_still_get_flagged(self):
        self.assertEqual(SpellNotifier().check("becuase"), (True, "because"))


if __name__ == "__main__":
    unittest.main()
