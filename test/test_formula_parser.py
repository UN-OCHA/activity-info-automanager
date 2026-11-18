from dataclasses import FrozenInstanceError
import unittest

from formula_parser import FormulaSyntaxError, includes, normalize, parse


class FormulaParserTests(unittest.TestCase):
    def equivalent(self, left, right):
        self.assertEqual(normalize(parse(left)), normalize(parse(right)))

    def test_whitespace_grouping_and_logical_order(self):
        for left, right in (
            (' A > 0 ', '((A)>(0))'),
            ('A && B', '( B ) AND (( A ))'),
            ('(A && B) && C', 'C && (B && A)'),
            ('A OR (B || C)', '(C OR A) || B'),
            ('A && (B || C)', '(C OR B) AND A'),
            ('A && A', 'A'),
            ('SUM( A, (B) ) > 0', 'SUM(A,B)>0'),
        ):
            with self.subTest(left=left, right=right):
                self.equivalent(left, right)

    def test_parentheses_preserve_meaning_and_nonlogical_order(self):
        for left, right in (
            ('A && (B || C)', '(A && B) || C'),
            ('A + (B * C)', '(A + B) * C'),
            ('A - (B - C)', '(A - B) - C'),
            ('A + B', 'B + A'),
            ('SUM(A,B)', 'SUM(B,A)'),
            ('x > 0', '0 < x'),
            ('[Field name]', '[Field  name]'),
            ('"A B"', '"AB"'),
        ):
            with self.subTest(left=left, right=right):
                self.assertNotEqual(normalize(parse(left)), normalize(parse(right)))

    def test_precedence_negation_and_references(self):
        self.equivalent('A || B && C', 'A || (B && C)')
        self.equivalent('!A && B', '(!A) && B')
        self.equivalent('NOT(A || B)', '!(A || B)')
        self.equivalent('NOT A > 0 AND B', '!(A > 0) && B')
        self.assertNotEqual(normalize(parse('NOT A > 0')), normalize(parse('(!A) > 0')))
        self.equivalent('2 ^ 3 ^ 4', '2 ^ (3 ^ 4)')
        self.equivalent('-2 ^ 2', '-(2 ^ 2)')
        self.equivalent('@parent.REF.[Field name] >= -.5e+2',
                        ' @parent . REF . [Field name] >= - .5e+2 ')

    def test_quoted_operators_doubled_quotes_and_escapes_are_opaque(self):
        for value in ('"A && (B)"', "'A OR B'", '"A "" OR "" B"', r'"A \" && B"'):
            with self.subTest(value=value):
                expression = parse(value + ' == field')
                self.assertEqual(expression.children[0].value, value)
        self.assertFalse(includes(parse('"A && B" == field'), parse('A'), 'AND'))
        parse('IF(A > 0, "text || with )", SUM(B, C)) != ""')

    def test_inclusion_uses_requested_top_level_operator(self):
        for current, candidate, operator, expected in (
            ('A && B', 'A', 'AND', True),
            ('B && A', '(A)', 'and', True),
            ('A && B && C', 'B && A', 'AND', True),
            ('A || B || C', 'B OR A', 'OR', True),
            ('A || B', 'A', 'AND', False),
            ('A && B', 'A', 'OR', False),
            ('A && B', 'B && A', 'OR', True),
            ('A && (B || C)', 'C || B', 'AND', True),
            ('A || (B && C)', 'B', 'AND', False),
            ('A', 'A && B', 'AND', False),
            ('A && (B || C)', '(A && B) || (A && C)', 'AND', False),
        ):
            with self.subTest(current=current, candidate=candidate, operator=operator):
                self.assertEqual(includes(parse(current), parse(candidate), operator), expected)
        with self.assertRaises(ValueError):
            includes(parse('A'), parse('A'), 'XOR')

    def test_malformed_and_unsupported_syntax_is_rejected(self):
        for value in ('', ' ', 'A &&', '|| A', '(A', 'A)', 'A B', 'A & B', 'A | B',
                      '"unclosed', '[unclosed', '[]', 'SUM(A,)', 'A..B', '1(2)',
                      'A > B > C', 'A ? B : C', 'AND A', 'A && ()', 'SUM(,)'):
            with self.subTest(value=value), self.assertRaises(FormulaSyntaxError):
                parse(value)

    def test_errors_have_source_locations_and_nodes_are_immutable(self):
        with self.assertRaises(FormulaSyntaxError) as caught:
            parse('A &&\n)')
        self.assertEqual((caught.exception.line, caught.exception.column), (2, 1))
        self.assertEqual(caught.exception.offset, 5)
        expression = parse('A && B')
        with self.assertRaises(FrozenInstanceError):
            expression.value = 'OR'
        self.assertIsInstance(expression.children, tuple)
        self.assertEqual(normalize(normalize(expression)), normalize(expression))


if __name__ == '__main__':
    unittest.main()
