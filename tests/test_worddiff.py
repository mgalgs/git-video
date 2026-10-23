import unittest

from git_video import worddiff


def by_kind(ops, kind):
    return [o for o in ops if o[0] == kind]


class TestWordDiff(unittest.TestCase):
    def test_modified_line(self):
        ops = worddiff.ops(['    return x + 1'], ['    return x * 2'])
        self.assertEqual(by_kind(ops, 'equal'), [['equal', 0, 4, 0, 4, 'return'], ['equal', 0, 11, 0, 11, 'x']])
        self.assertEqual([o[5] for o in by_kind(ops, 'delete')], ['+', '1'])
        self.assertEqual([o[5] for o in by_kind(ops, 'insert')], ['*', '2'])
        self.assertEqual(by_kind(ops, 'delete')[0][1:5], [0, 13, -1, -1])
        self.assertEqual(by_kind(ops, 'insert')[0][1:5], [-1, -1, 0, 13])

    def test_kept_words_move(self):
        ops = worddiff.ops(['for a in argv:'], ['i = 0', 'while a in argv:'])
        (argv,) = [o for o in by_kind(ops, 'equal') if o[5] == 'argv']
        self.assertEqual(argv[1:5], [0, 9, 1, 11])

    def test_pure_delete(self):
        ops = worddiff.ops(['def f():', '    pass'], [])
        self.assertEqual({o[0] for o in ops}, {'delete'})
        self.assertEqual([o[5] for o in ops], ['def', 'f', '(', ')', ':', 'pass'])

    def test_pure_add(self):
        ops = worddiff.ops([], ['x = 1'])
        self.assertEqual({o[0] for o in ops}, {'insert'})

    def test_whitespace_only_change_has_no_ops_but_equals(self):
        ops = worddiff.ops(['a  =  1'], ['a = 1'])
        self.assertEqual({o[0] for o in ops}, {'equal'})

    def test_words_match_only_within_an_edited_line(self):
        # line 0 is edited (most words kept); line 1 has nothing to do with the old
        # line 1 but shares `=` and `(`: those must not fly between the two
        ops = worddiff.ops(['x = foo(a, b)', 'y = 2'], ['x = bar(a, b)', 'total = compute(q)'])
        self.assertTrue(all(o[1] == o[3] == 0 for o in by_kind(ops, 'equal')))
        self.assertEqual([o[5] for o in by_kind(ops, 'insert') if o[3] == 1], ['total', '=', 'compute', '(', 'q', ')'])
        self.assertEqual([o[5] for o in by_kind(ops, 'delete') if o[1] == 1], ['y', '=', '2'])
        self.assertEqual(worddiff.pair_rows(['x = foo(a, b)', 'y = 2'], ['x = bar(a, b)', 'total = compute(q)']),
                         [(0, 0)])

    def test_too_many_tokens(self):
        self.assertIsNone(worddiff.ops(['x ' * worddiff.MAX_TOKENS], ['y']))


if __name__ == '__main__':
    unittest.main()
