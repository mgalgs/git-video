import unittest

from git_video import callouts


def info(path, words=(), defs=()):
    return {'path': path, 'words': set(words), 'defs': set(defs)}


class TestCallouts(unittest.TestCase):
    def test_sentences(self):
        body = ('First one. Second one, which\nwraps! Third?\n\n'
                '- a bullet\n  that continues\n* another\n1. numbered\n\nLast paragraph.')
        self.assertEqual(callouts.sentences(body), ['First one.', 'Second one, which wraps!', 'Third?',
                                                   'a bullet that continues', 'another', 'numbered',
                                                   'Last paragraph.'])
        self.assertEqual(callouts.sentences(''), [])

    def test_identifiers(self):
        ids = callouts.identifiers('`file_rows()` and sidebar.draw with --verbose touch pyproject.toml, '
                                   'getValue() and fooBar, e.g. the file test helper, frobnicate')
        self.assertEqual(ids, ['file_rows', 'sidebar.draw', 'sidebar', '--verbose', 'pyproject.toml', 'pyproject',
                               'getValue', 'fooBar'])
        self.assertEqual(callouts.identifiers('the file test helper'), [])
        self.assertEqual(callouts.identifiers('then frobnicate it', names={'frobnicate'}), ['frobnicate'])
        self.assertEqual(callouts.identifiers('`a = 1` and `x`'), [])   # too short to be anything

    def test_function_lands_on_its_hunk(self):
        hunks = [info('a.py', words={'x', 'y'}, defs={'other'}), info('b.py', words={'frob'}, defs={'frob'})]
        self.assertEqual(callouts.match(['Now frob() doubles.'], hunks), [(0, 1, 'frob', 5)])

    def test_unmatched_lands_nowhere(self):
        hunks = [info('a.py', words={'frob'}, defs={'frob'})]
        self.assertEqual(callouts.match(['Nothing to see here.', 'The helper_fn is gone.'], hunks), [])

    def test_tie_goes_to_first_hunk(self):
        hunks = [info('a.py', words={'frob'}), info('b.py', words={'frob'})]
        self.assertEqual(callouts.match(['`frob` changed.'], hunks)[0][1], 0)

    def test_file_name_and_flag(self):
        hunks = [info('cli.py', words={'verbose'}), info('pyproject.toml', words={'license'})]
        out = callouts.match(['`--verbose 2` works.', 'pyproject.toml lost its `license`.'], hunks)
        self.assertEqual([(u, h) for u, h, _, _ in out], [(0, 0), (1, 1)])


if __name__ == '__main__':
    unittest.main()
