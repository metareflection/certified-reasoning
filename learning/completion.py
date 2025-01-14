#!/usr/bin/env python3

import copy
import regex
import unittest

from typing import Optional

import domain
import util
from synchromesh import StreamingCSD


def regex_not_containing(m):
    'Returns a regular expression for any string that does not contain m.'
    options = []

    for i in range(len(m)):
        options.append(f'{regex.escape(m[:i])}[^{regex.escape(m[i])}]')
    return f'({"|".join(options)})*'


def _split_block(b: str) -> (str, str):
    colon = b.index(':')
    return (b[:colon], b[colon+1:])


# Error when nothing to infer
INFER_ERROR = 'nothing'


class PeanoCompletionEngine:
    '''CSD completion engine backed by a Peano domain.'''
    def __init__(self, domain, start_derivation,
                 format_fn=lambda s: s, start_marker='[[', end_marker=']]',
                 infer_atoms=True, done_when_exhausted=False):
        self.domain = domain
        self.start_derivation = start_derivation
        self.start_marker = start_marker
        self.end_marker = end_marker
        self.format_fn = format_fn
        self.infer_atoms = infer_atoms
        self.done_when_exhausted = done_when_exhausted

    def _get_open_block(self, prefix: str) -> Optional[str]:
        # Find last occurrence of start and end markers.
        last_start = prefix.rfind(self.start_marker)
        last_end = prefix.rfind(self.end_marker)

        # No start marker yet, or has both but last block was already closed.
        if last_start == -1 or (last_end != -1 and
                                last_end > last_start):
            return None

        # Otherwise, last open block is still open.
        return prefix[last_start + len(self.start_marker):]

    def complete(self, prefix: str):
        b = self._get_open_block(prefix)
        end_marker = regex.escape(self.end_marker)

        if b is None:
            # Match anything not containing the start marker, followed
            # by the start marker.
            return regex.compile(regex_not_containing(self.start_marker) +
                                 regex.escape(self.start_marker))

        if not b:
            # The block was just open: return the supported keywords.
            previous_blocks = {b for b, _ in self.get_verified_blocks(prefix)}
            allowed_next = ['prop', 'object', 'var', 'relation', 'axiom']

            if self.infer_atoms or \
               'axiom' in previous_blocks:
                allowed_next.append('infer')
                allowed_next.append('goal')

            if 'var' in previous_blocks:
                allowed_next.append('eq')

            block_keywords = f'({"|".join(allowed_next)}):'
            return regex.compile(block_keywords)

        block_keyword, block_contents = _split_block(b)
        assert not block_contents

        if block_keyword in ('prop', 'object', 'relation'):
            return regex.compile('[a-zA-Z0-9-_]{1,50}' + end_marker)

        if block_keyword == 'var':
            return regex.compile('[a-z0-9-_]{1,50}' + end_marker)

        if block_keyword == 'eq':
            return regex.compile('[\\(\\)a-zA-Z0-9\\-=+\\*/ ]+' + end_marker)

        if block_keyword in ('axiom', 'goal'):
            if self.infer_atoms:
                return self._make_freeform_proposition_regex(block_keyword == 'goal')

            previous_blocks = self.get_verified_blocks(prefix)
            return self._make_proposition_regex(previous_blocks,
                                                block_keyword == 'goal')

        regex.compile(regex_not_containing(self.end_marker) +
                      end_marker)

        if block_keyword == 'infer':
            # Match any of the actions followed by the end marker.
            verified_blocks = self.get_verified_blocks(prefix)
            ff_derivation = self.fast_forward_derivation(verified_blocks)
            choices = self.enumerate_choices(ff_derivation.universe)

            # Filter duplicate inferences.
            new_choices = []
            for c in choices:
                inference = self.format_fn(c.clean_dtype(ff_derivation.universe))
                is_new = True

                for keyword, content in verified_blocks:
                    if keyword == 'infer' and content == inference:
                        is_new = False
                        break

                if is_new:
                    new_choices.append(inference)

            if not new_choices:
                new_choices = [INFER_ERROR]

            out = '|'.join(map(regex.escape, new_choices))

            return regex.compile(f'({out}){end_marker}')

        raise ValueError(f'Invalid block type {block_keyword}.')

    def _make_freeform_proposition_regex(self, is_goal: bool) -> regex.Regex:
        atom_regex = '([a-zA-Z0-9-_]{1,50})'
        param_regex = '(\'[a-z])'

        if is_goal:
            argument_regex = atom_regex
        else:
            argument_regex = f'({param_regex}|{atom_regex})'

        term_regex = rf'({argument_regex}|(\({atom_regex}( {argument_regex}){{1,2}}\)))'
        positive_property = rf'(\({atom_regex}( {term_regex}){{1,2}}\))'
        proposition = rf'({positive_property}|(\(not {positive_property}\)))'

        if is_goal:
            return regex.compile(proposition + regex.escape(self.end_marker))

        return regex.compile(f'{proposition}( -> ({proposition}))*{regex.escape(self.end_marker)}')

    def _make_proposition_regex(self,
                                previous_blocks: list[tuple[str, str]],
                                is_goal: bool) -> regex.Regex:
        objects = [v for k, v in previous_blocks if k == 'object']
        props = [v for k, v in previous_blocks if k == 'prop']

        premises, conclusions = [], []

        for p in props:
            for o in objects + ["'x"]:
                positive_prop = f'({p} {o})'
                negative_prop = f'(not ({p} {o}))'

                premises.append(positive_prop)
                premises.append(negative_prop)

                if not o.startswith("'"):
                    conclusions.append(positive_prop)
                    conclusions.append(negative_prop)

        all_premises = '|'.join(map(regex.escape, premises))
        all_conclusions = '|'.join(map(regex.escape, conclusions))

        if not all_conclusions:
            # A regex for the empty formal language.
            all_conclusions = '$.'

        if not all_premises:
            assert False, 'Empty theory'

        end_marker = regex.escape(self.end_marker)

        if is_goal:
            return regex.compile(f'({all_conclusions}){end_marker}')

        return regex.compile(f'(({all_conclusions})|' +
                             f'(({all_premises}) -> )+({all_premises}))' +
                             end_marker)


    def fast_forward_derivation(self, verified_blocks: list[tuple[str, str]]):
        u = self.start_derivation.universe.clone()

        u.incorporate('object : type. not : [prop -> prop].')
        arities = {}

        goal = None

        for i, (block_type, block_content) in enumerate(verified_blocks):
            if block_type == 'prop':
                if not self.infer_atoms:
                    u.incorporate(f'{block_content} : [object -> prop].')
            elif block_type == 'relation':
                if not self.infer_atoms:
                    u.incorporate(f'{block_content} : [object -> object -> prop].')
            elif block_type == 'axiom':
                if self.infer_atoms:
                    # Infer arities and declare new things.
                    ignore = False
                    for prop in block_content.split(' -> '):
                        new_arities, toplevel = infer_arities(prop)

                        for k, v in new_arities.items():
                            if k not in arities:
                                # found new atom; declare it with the inferred arity.
                                arities[k] = v

                                if v == 0:
                                    u.incorporate(f'let {k} : object.')
                                else:
                                    rtype = 'prop' if k == toplevel else 'object'
                                    u.incorporate(f'{k} : [{" -> ".join(["object"] * v)} -> {rtype}].')
                            elif v != arities[k]:
                                ignore = True
                                break
                        if ignore:
                            break

                    if ignore:
                        continue

                # Wrap arrow types.
                if block_content.find('->') != -1:
                    block_content = f'[{block_content}]'

                u.incorporate(f'axiom{i} : {block_content}.')
            elif block_type == 'object':
                if not self.infer_atoms:
                    u.incorporate(f'let {block_content} : object.')
            elif block_type == 'goal':
                goal = block_content
            elif block_type == 'var':
                u.incorporate(f'let {block_content} : real.')
            elif block_type == 'eq':
                u.incorporate(f'eq{i} : {util.format_infix(block_content)}.')
            elif block_type == 'infer':
                choices = self.enumerate_choices(u)

                found = False
                for c in choices:
                    if self.format_fn(self.domain.value_of(u, c)) == block_content:
                        # Found the choice made at this step.
                        found = True
                        self.domain.define(u, f'!step{i}', c)
                        break

                if block_content != INFER_ERROR:
                    assert found, f'Could not replay inference in verified block {block_content}.'
            else:
                raise ValueError(f'Invalid block type {block_type}')

        d_prime = copy.copy(self.start_derivation)
        d_prime.universe = u
        d_prime.goal = goal

        return d_prime


    def enumerate_choices(self, universe):
        initial_actions = set(self.domain.derivation_actions(self.start_derivation.universe) +
                              self.domain.tactic_actions())
        arrows = set(self.domain.derivation_actions(universe)).union(initial_actions)

        choices = []

        for a in arrows:
            if a in initial_actions or regex.fullmatch('axiom\\d+', a):
                choices.extend(self.domain.apply(a, universe))

        return choices

    def get_verified_blocks(self, prefix: str) -> list[tuple[str, str]]:
        blocks, i = [], None

        while True:
            i = prefix.find(self.start_marker, i)
            if i != -1:
                j = prefix.find(self.end_marker, i)
                if j != -1:
                    blocks.append(
                        _split_block(prefix[i + len(self.start_marker):j]))
                    i = j + 1
                else:
                    break
            else:
                break

        seen_blocks = set()
        unique_blocks = []

        for b in blocks:
            if b not in seen_blocks:
                seen_blocks.add(b)
                unique_blocks.append(b)

        return unique_blocks

    def is_complete(self, prefix: str) -> bool:
        blocks = self.get_verified_blocks(prefix)

        # If exhausted inferences, it is done.
        if INFER_ERROR in [v for k, v in blocks]:
            if self.done_when_exhausted:
                return True, False

            return None, False

        ff = self.fast_forward_derivation(blocks)
        return self.domain.derivation_done(ff)


def infer_sexp_arities(sexp: list, result: dict[str, int]):
    if isinstance(sexp, str):
        if result.setdefault(sexp, 0) != 0:
            raise ValueError(f'Conflicting arities for {sexp}.')
    else:
        fn = sexp[0]
        arity = len(sexp) - 1

        if fn != 'not':
            if result.setdefault(fn, arity) != arity:
                raise ValueError(f'Conflicting arities for {fn}.')

        for subexpr in sexp[1:]:
            infer_sexp_arities(subexpr, result)


def infer_arities(rule: str) -> dict[str, int]:
    result = {}
    sexp, _ = util.parse_sexp(rule)
    infer_sexp_arities(sexp, result)
    if sexp[0] == 'not' and len(sexp) == 2:
        toplevel = sexp[1][0] if isinstance(sexp[1], list) else sexp[1]
    else:
        toplevel = sexp[0]
    return result, toplevel


class PeanoCompletionEngineTest(unittest.TestCase):
    def test_fol_completions(self):
        d = domain.FirstOrderLogicDomain()
        prob = d.start_derivation()

        ce = PeanoCompletionEngine(d, prob)

        p1 = '''
1- Vumpuses are zumpuses. 2- Each zumpus is a rompus. 3- Every tumpus is small. 4- Each impus is a tumpus. 5- Each rompus is a jompus. 6- Tumpuses are wumpuses. 7- Every yumpus is transparent. 8- Yumpuses are numpuses. 9- Zumpuses are orange. 10- Jompuses are yumpuses. 11- Rompuses are floral. 12- Wumpuses are vumpuses. 13- Every wumpus is nervous. 14- Every impus is temperate. 15- Jompuses are not sweet. 16- Dumpuses are not floral. 17- Every vumpus is angry. 18- Sally is a tumpus.
Query: True or false: Sally is not floral.
Formalized context: 1- [[prop:vumpus]] are [[prop:zumpus]]. [[axiom:(vumpus 'x) -> (zumpus 'x)]]. 2- Each [[prop:zumpus]] is a [[prop:rompus]]. [[axiom:(zumpus 'x) -> (rompus 'x)]]. 3- Every [[prop:tumpus]] is [[prop:small]]. [[axiom:(tumpus 'x) -> (small 'x)]]. 4- Each [[prop:impus]] is a [[prop:tumpus]]. [[axiom:(impus 'x) -> (tumpus 'x)]]. 5- Each [[prop:rompus]] is a [[prop:jompus]]. [[axiom:(rompus 'x) -> (jompus 'x)]]. 6- [[prop:tumpus]] are [[prop:wumpus]]. [[axiom:(tumpus 'x) -> (wumpus 'x)]]. 7- Every [[prop:yumpus]] is [[prop:transparent]]. [[axiom:(yumpus 'x) -> (transparent 'x)]]. 8- [[prop:yumpus]] are [[prop:numpus]]. [[axiom:(yumpus 'x) -> (numpus 'x)]]. 9- [[prop:zumpus]] are [[prop:orange]]. [[axiom:(zumpus 'x) -> (orange 'x)]]. 10- [[prop:jompus]] are [[prop:yumpus]]. [[axiom:(jompus 'x) -> (yumpus 'x)]]. 11- [[prop:rompus]] are [[prop:floral]]. [[axiom:(rompus 'x) -> (floral 'x)]]. 12- [[prop:wumpus]] are [[prop:vumpus]]. [[axiom:(wumpus 'x) -> (vumpus 'x)]]. 13- Every [[prop:wumpus]] is [[prop:nervous]]. [[axiom:(wumpus 'x) -> (nervous 'x)]]. 14- Every [[prop:impus]] is [[prop:temperate]]. [[axiom:(impus 'x) -> (temperate 'x)]]. 15- [[prop:jompus]] are not [[prop:sweet]]. [[axiom:(jompus 'x) -> (not (sweet 'x))]]. 16- [[prop:dumpus]] are not [[prop:floral]]. [[axiom:(dumpus 'x) -> (not (floral 'x))]]. 17- Every [[prop:vumpus]] is [[prop:angry]]. [[axiom:(vumpus 'x) -> (angry 'x)]]. 18- [[object:sally]] is a [[prop:tumpus]]. [[axiom:(tumpus sally)]].
Formalized goal: [[goal:(not (floral sally))]]
Reasoning: [[infer:'''

        self.assertFalse(ce.is_complete(p1))

        completions = ce.complete(p1)

        self.assertTrue(completions.match('(wumpus sally)]]'))
        self.assertFalse(completions.match('(rompus sally)]]'))

        p2 = '''
1- Vumpuses are zumpuses. 2- Each zumpus is a rompus. 3- Every tumpus is small. 4- Each impus is a tumpus. 5- Each rompus is a jompus. 6- Tumpuses are wumpuses. 7- Every yumpus is transparent. 8- Yumpuses are numpuses. 9- Zumpuses are orange. 10- Jompuses are yumpuses. 11- Rompuses are floral. 12- Wumpuses are vumpuses. 13- Every wumpus is nervous. 14- Every impus is temperate. 15- Jompuses are not sweet. 16- Dumpuses are not floral. 17- Every vumpus is angry. 18- Sally is a tumpus.
Query: True or false: Sally is not floral.
Formalized context: 1- [[prop:vumpus]] are [[prop:zumpus]]. [[axiom:(vumpus 'x) -> (zumpus 'x)]]. 2- Each [[prop:zumpus]] is a [[prop:rompus]]. [[axiom:(zumpus 'x) -> (rompus 'x)]]. 3- Every [[prop:tumpus]] is [[prop:small]]. [[axiom:(tumpus 'x) -> (small 'x)]]. 4- Each [[prop:impus]] is a [[prop:tumpus]]. [[axiom:(impus 'x) -> (tumpus 'x)]]. 5- Each [[prop:rompus]] is a [[prop:jompus]]. [[axiom:(rompus 'x) -> (jompus 'x)]]. 6- [[prop:tumpus]] are [[prop:wumpus]]. [[axiom:(tumpus 'x) -> (wumpus 'x)]]. 7- Every [[prop:yumpus]] is [[prop:transparent]]. [[axiom:(yumpus 'x) -> (transparent 'x)]]. 8- [[prop:yumpus]] are [[prop:numpus]]. [[axiom:(yumpus 'x) -> (numpus 'x)]]. 9- [[prop:zumpus]] are [[prop:orange]]. [[axiom:(zumpus 'x) -> (orange 'x)]]. 10- [[prop:jompus]] are [[prop:yumpus]]. [[axiom:(jompus 'x) -> (yumpus 'x)]]. 11- [[prop:rompus]] are [[prop:floral]]. [[axiom:(rompus 'x) -> (floral 'x)]]. 12- [[prop:wumpus]] are [[prop:vumpus]]. [[axiom:(wumpus 'x) -> (vumpus 'x)]]. 13- Every [[prop:wumpus]] is [[prop:nervous]]. [[axiom:(wumpus 'x) -> (nervous 'x)]]. 14- Every [[prop:impus]] is [[prop:temperate]]. [[axiom:(impus 'x) -> (temperate 'x)]]. 15- [[prop:jompus]] are not [[prop:sweet]]. [[axiom:(jompus 'x) -> (not (sweet 'x))]]. 16- [[prop:dumpus]] are not [[prop:floral]]. [[axiom:(dumpus 'x) -> (not (floral 'x))]]. 17- Every [[prop:vumpus]] is [[prop:angry]]. [[axiom:(vumpus 'x) -> (angry 'x)]]. 18- [[object:sally]] is a [[prop:tumpus]]. [[axiom:(tumpus sally)]].
Formalized goal: [[goal:(not (floral sally))]]
Reasoning: [[infer:(wumpus sally)]] Sally is a wumpus.
            [[infer:(vumpus sally)]] Sally is a vumpus. [[infer:(zumpus sally)]] Sally is a zumpus.
            [[infer:(rompus sally)]] Sally is a rompus. [[infer:'''

        completions = ce.complete(p2)

        self.assertFalse(ce.is_complete(p2))

        self.assertTrue(completions.match('(floral sally)]]'))
        self.assertFalse(completions.match('(not (floral sally))]]'))

        p3 = '''
Context: 1- Vumpuses are zumpuses. 2- Each zumpus is a rompus. 3- Every tumpus is small. 4- Each impus is a tumpus. 5- Each rompus is a jompus. 6- Tumpuses are wumpuses. 7- Every yumpus is transparent. 8- Yumpuses are numpuses. 9- Zumpuses are orange. 10- Jompuses are yumpuses. 11- Rompuses are floral. 12- Wumpuses are vumpuses. 13- Every wumpus is nervous. 14- Every impus is temperate. 15- Jompuses are not sweet. 16- Dumpuses are not floral. 17- Every vumpus is angry. 18- Sally is a tumpus.
Query: True or false: Sally is not floral.
Formalized context: 1- [[prop:vumpus]] are [[prop:zumpus]]. [[axiom:(vumpus 'x) -> (zumpus 'x)]]. 2- Each [[prop:zumpus]] is a [[prop:rompus]]. [[axiom:(zumpus 'x) -> (rompus 'x)]]. 3- Every [[prop:tumpus]] is [[prop:small]]. [[axiom:(tumpus 'x) -> (small 'x)]]. 4- Each [[prop:impus]] is a [[prop:tumpus]]. [[axiom:(impus 'x) -> (tumpus 'x)]]. 5- Each [[prop:rompus]] is a [[prop:jompus]]. [[axiom:(rompus 'x) -> (jompus 'x)]]. 6- [[prop:tumpus]] are [[prop:wumpus]]. [[axiom:(tumpus 'x) -> (wumpus 'x)]]. 7- Every [[prop:yumpus]] is [[prop:transparent]]. [[axiom:(yumpus 'x) -> (transparent 'x)]]. 8- [[prop:yumpus]] are [[prop:numpus]]. [[axiom:(yumpus 'x) -> (numpus 'x)]]. 9- [[prop:zumpus]] are [[prop:orange]]. [[axiom:(zumpus 'x) -> (orange 'x)]]. 10- [[prop:jompus]] are [[prop:yumpus]]. [[axiom:(jompus 'x) -> (yumpus 'x)]]. 11- [[prop:rompus]] are [[prop:floral]]. [[axiom:(rompus 'x) -> (floral 'x)]]. 12- [[prop:wumpus]] are [[prop:vumpus]]. [[axiom:(wumpus 'x) -> (vumpus 'x)]]. 13- Every [[prop:wumpus]] is [[prop:nervous]]. [[axiom:(wumpus 'x) -> (nervous 'x)]]. 14- Every [[prop:impus]] is [[prop:temperate]]. [[axiom:(impus 'x) -> (temperate 'x)]]. 15- [[prop:jompus]] are not [[prop:sweet]]. [[axiom:(jompus 'x) -> (not (sweet 'x))]]. 16- [[prop:dumpus]] are not [[prop:floral]]. [[axiom:(dumpus 'x) -> (not (floral 'x))]]. 17- Every [[prop:vumpus]] is [[prop:angry]]. [[axiom:(vumpus 'x) -> (angry 'x)]]. 18- [[object:sally]] is a [[prop:tumpus]]. [[axiom:(tumpus sally)]].
Formalized goal: [[goal:(not (floral sally))]]
Reasoning: [[infer:(wumpus sally)]] Sally is a wumpus. [[infer:(vumpus sally)]] Sally is a vumpus.
        [[infer:(zumpus sally)]] Sally is a zumpus. [[infer:(rompus sally)]] Sally is a rompus.
        [[infer:(floral sally)]] Sally is floral. This contradicts the goal.
        '''

        self.assertTrue(ce.is_complete(p3))


    def test_axiom_constraints(self):
        d = domain.FirstOrderLogicDomain()
        prob = d.start_derivation()

        ce = PeanoCompletionEngine(d, prob, infer_atoms=False)

        p1 = '''
1- Vumpuses are zumpuses. 2- Each zumpus is a rompus. 3- Every tumpus is small. 4- Each impus is a tumpus. 5- Each rompus is a jompus. 6- Tumpuses are wumpuses. 7- Every yumpus is transparent. 8- Yumpuses are numpuses. 9- Zumpuses are orange. 10- Jompuses are yumpuses. 11- Rompuses are floral. 12- Wumpuses are vumpuses. 13- Every wumpus is nervous. 14- Every impus is temperate. 15- Jompuses are not sweet. 16- Dumpuses are not floral. 17- Every vumpus is angry. 18- Sally is a tumpus.
Query: True or false: Sally is not floral.
Formalized context: 1- [[prop:vumpus]] are [[prop:zumpus]]. [[axiom:'''

        r = ce.complete(p1)

        self.assertTrue(r.fullmatch("(vumpus 'x) -> (zumpus 'x)]]"))
        self.assertTrue(r.fullmatch("(not (vumpus 'x)) -> (zumpus 'x)]]"))
        self.assertFalse(r.match("(not (vumpus 'x))]]"))

        p2 = '''
1- Vumpuses are zumpuses. 2- Each zumpus is a rompus. 18- Sally is a vumpus.
Query: True or false: Sally is not floral.
Formalized context: 1- [[prop:vumpus]] are [[prop:zumpus]]. [[axiom:(vumpus 'x) -> (zumpus 'x)]]. 2- [[object:sally]] is a [[prop:vumpus]]. [[axiom:'''

        r2 = ce.complete(p2)

        self.assertTrue(r2.fullmatch("(vumpus sally)]]"))
        self.assertFalse(r2.fullmatch("(vumpus 'x)]]"))

        p3 = '''
1- Vumpuses are zumpuses. 2- Each zumpus is a rompus. 18- Sally is a vumpus.
Query: True or false: Sally is not floral.
Formalized context: 1- [[prop:vumpus]] are [[prop:zumpus]]. [[axiom:(vumpus 'x) -> (zumpus 'x)]]. 2- [[object:sally]] is a [[prop:vumpus]]. [[axiom:(vumpus sally)]]. Goal: [[goal:'''

        r3 = ce.complete(p3)

        self.assertTrue(r3.fullmatch("(vumpus sally)]]"))
        self.assertTrue(r3.fullmatch("(zumpus sally)]]"))
        self.assertFalse(r3.fullmatch("(nompus sally)]]"))


    def test_empty_infer_options(self):
        prefix = """1- Every [[prop:feline]] is a [[prop:carnivore]]. [[axiom:(feline 'x) -> (carnivore 'x)]]. 2- [[object:sheep]] are not [[prop:carnivorous]]. [[axiom:(not (carnivorous sheep))]]. 3- Each [[prop:carnivore]] is a [[prop:mammal]]. [[axiom:(carnivore 'x) -> (mammal 'x)]]. 4- [[object:cats]] are [[prop:feline]]. [[axiom:(feline cats)]]. 5- Each [[prop:mammal]] is [[prop:furry]]. [[axiom:(mammal 'x) -> (furry 'x)]]. 6- Every [[prop:carnivore]] is [[prop:carnivorous]]. [[axiom:(carnivore 'x) -> (carnivorous 'x)]]. 7- Every [[prop:mammal]] is a [[prop:vertebrate]]. [[axiom:(mammal 'x) -> (vertebrate 'x)]]. 8- [[prop:animal]] are not [[prop:unicellular]]. [[axiom:(animal 'x) -> (not (unicellular 'x))]]. 9- [[prop:vertebrate]] are [[prop:animal]]. [[axiom:(vertebrate 'x) -> (animal 'x)]]. 10- [[object:stella]] is a [[object:cat]]. [[axiom:(not (animal stella))]].
Formalized goal: [[goal:(carnivorous stella)]]
Reasoning: [[infer:(carnivore cats)]] Cats are carnivores. [[infer:(carnivorous cats)]] Cats are carnivorous. [[infer:(mammal cats)]] Cats are mammals. [[infer:(vertebrate cats)]] Cats are vertebrates. [[infer:(animal cats)]] Cats are animals. [[infer:(not (unicellular cats))]] Cats are not unicellular. [[infer:(furry cats)]] Cats are furry. [[infer:"""

        d = domain.FirstOrderLogicDomain()
        prob = d.start_derivation()
        ce = PeanoCompletionEngine(d, prob)

        a = ce.complete(prefix)
        self.assertTrue(a.match(INFER_ERROR, partial=True))


    def test_avoid_duplicates(self):
        d = domain.FirstOrderLogicDomain()
        prob = d.start_derivation()

        ce = PeanoCompletionEngine(d, prob)

        prefix = '''
Context: 1- Vumpuses are zumpuses. 2- Each zumpus is a rompus. 3- Every tumpus is small. 4- Each impus is a tumpus. 5- Each rompus is a jompus. 6- Tumpuses are wumpuses. 7- Every yumpus is transparent. 8- Yumpuses are numpuses. 9- Zumpuses are orange. 10- Jompuses are yumpuses. 11- Rompuses are floral. 12- Wumpuses are vumpuses. 13- Every wumpus is nervous. 14- Every impus is temperate. 15- Jompuses are not sweet. 16- Dumpuses are not floral. 17- Every vumpus is angry. 18- Sally is a tumpus.
Query: True or false: Sally is not floral.
Formalized context: 1- [[prop:vumpus]] are [[prop:zumpus]]. [[axiom:(vumpus 'x) -> (zumpus 'x)]]. 2- Each [[prop:zumpus]] is a [[prop:rompus]]. [[axiom:(zumpus 'x) -> (rompus 'x)]]. 3- Every [[prop:tumpus]] is [[prop:small]]. [[axiom:(tumpus 'x) -> (small 'x)]]. 4- Each [[prop:impus]] is a [[prop:tumpus]]. [[axiom:(impus 'x) -> (tumpus 'x)]]. 5- Each [[prop:rompus]] is a [[prop:jompus]]. [[axiom:(rompus 'x) -> (jompus 'x)]]. 6- [[prop:tumpus]] are [[prop:wumpus]]. [[axiom:(tumpus 'x) -> (wumpus 'x)]]. 7- Every [[prop:yumpus]] is [[prop:transparent]]. [[axiom:(yumpus 'x) -> (transparent 'x)]]. 8- [[prop:yumpus]] are [[prop:numpus]]. [[axiom:(yumpus 'x) -> (numpus 'x)]]. 9- [[prop:zumpus]] are [[prop:orange]]. [[axiom:(zumpus 'x) -> (orange 'x)]]. 10- [[prop:jompus]] are [[prop:yumpus]]. [[axiom:(jompus 'x) -> (yumpus 'x)]]. 11- [[prop:rompus]] are [[prop:floral]]. [[axiom:(rompus 'x) -> (floral 'x)]]. 12- [[prop:wumpus]] are [[prop:vumpus]]. [[axiom:(wumpus 'x) -> (vumpus 'x)]]. 13- Every [[prop:wumpus]] is [[prop:nervous]]. [[axiom:(wumpus 'x) -> (nervous 'x)]]. 14- Every [[prop:impus]] is [[prop:temperate]]. [[axiom:(impus 'x) -> (temperate 'x)]]. 15- [[prop:jompus]] are not [[prop:sweet]]. [[axiom:(jompus 'x) -> (not (sweet 'x))]]. 16- [[prop:dumpus]] are not [[prop:floral]]. [[axiom:(dumpus 'x) -> (not (floral 'x))]]. 17- Every [[prop:vumpus]] is [[prop:angry]]. [[axiom:(vumpus 'x) -> (angry 'x)]]. 18- [[object:sally]] is a [[prop:tumpus]]. [[axiom:(tumpus sally)]].
Formalized goal: [[goal:(not (floral sally))]]
Reasoning: [[infer:(wumpus sally)]] Sally is a wumpus. [[infer:'''

        self.assertTrue(ce.complete(prefix).match('(vumpus sally)]]'))
        # Duplicate
        self.assertFalse(ce.complete(prefix).match('(wumpus sally)]]'))

    def test_algebra_problem(self):
        import tactics

        d = domain.AlgebraDomain()
        d.load_tactics([
            tactics.Tactic(
                'eval_rewrite',
                [
                    tactics.Step(['eval'], ['?a@*'], '?0'),
                    tactics.Step(['rewrite'], ['?0', '?a@*'], '?1'),
                ]
            ),
            tactics.Tactic(
                'eval_rewrite_loop',
                [
                    tactics.Step(['eval_rewrite'], ['?a'], '?0'),
                    tactics.Step(['eval_rewrite_loop', 'eval_rewrite'], ['?0'], '?1'),
                ]
            )
        ])

        prob = d.start_derivation()

        ce = PeanoCompletionEngine(d, prob, util.format_infix)

        prefix = '''Problem #1
Context: Let x = f^3 + 2g - 4h. Suppose f = 2, g = 3 and h = 4.
Query: Find the value of x.
Formalized context: We have the following variables: [[var:x]], [[var:f]], [[var:g]] and [[var:h]]. We're also given the following equations: [[eq:(x = (((f * (f * f)) + (2 * g)) - (4 * h)))]], [[eq:(f = 2)]], [[eq:(g = 3)]] and finally [[eq:(h = 4)]].
Formalized goal: We want to find [[goal:x]].
Formal solution: Let's substitute until we have no more variables on the right-hand side. First we get [[infer:(x = (((2 * (f * f)) + (2 * g)) - (4 * h)))]]. Then, we get [[infer:(x = (((2 * (2 * f)) + (2 * g)) - (4 * h)))]]. Substituting one more, we get [[infer:(x = (((2 * (2 * 2)) + (2 * g)) - (4 * h)))]]. One more time and we get [[infer:(x = (((2 * (2 * 2)) + (2 * 3)) - (4 * h)))]]. Finally we get [[infer:(x = (((2 * (2 * 2)) + (2 * 3)) - (4 * 4)))]]. We can now start evaluating the expression. [[infer:(x = (((2 * 4) + (2 * 3)) - (4 * 4)))]]. [[infer:(x = ((8 + (2 * 3)) - (4 * 4)))]]. [[infer:(x = ((8 + 6) - (4 * 4)))]]. [[infer:(x = (14 - (4 * 4)))]]. [[infer:(x = (14 - 16))]]. '''

        self.assertFalse(ce.is_complete(prefix))

        prefix += '[[infer:(x = -2)]]'

        self.assertTrue(ce.is_complete(prefix))

        prefix = '''Problem #2
Context: Let x = 8 - m/n + p^2. Suppose m = 8, n = 2 and p = 7.
Query: Find the value of x.
Formalized context: We have the following variables: [[var:x]], [[var:m]], [[var:n]], and [[var:p]]. We're also given the following equations: [[eq:(x = ((8 - (m / n)) + (p * p)))]], [[eq:(m = 8)]], [[eq:(n = 2)]] and finally [[eq:(p = 7)]].
Formalized goal: We want to find [[goal:x]].
Formal solution: Let's substitute until we have no more variables on the right-hand side. First we get [[infer:(x = ((8 - (8 / n)) + (p * p)))]]. Then, we get [[infer:(x = ((8 - (8 / 2)) + (p * p)))]]. Substituting for p once, we get [[infer:(x = ((8 - (8 / 2)) + (7 * p)))]]. Finally we have [[infer:(x = ((8 - (8 / 2)) + (7 * 7)))]]. We can now evaluate. First we get [[infer:(x = ((8 - 4) + (7 * 7)))]]. Then we get [[infer:(x = (4 + (7 * 7)))]]. Finalizing, we get [[infer:(x = 53)]]. That is the answer.
Answer: 53'''
        self.assertTrue(ce.is_complete(prefix))


    def test_no_valid_tokens_bug(self):
        d = domain.FirstOrderLogicDomain()
        prob = d.start_derivation()

        ce = PeanoCompletionEngine(d, prob)

        prefix = """Formalized context: 1- [[prop:vumpus]] are [[prop:zumpus]]. [[axiom:(vumpus 'x) -> (zumpus 'x)]]. 2- Each [[prop:zumpus]] is a [[prop:rompus]]. [[axiom:(zumpus 'x) -> (rompus 'x)]]. 3- Every [[prop:tumpus]] is [[prop:small]]. [[axiom:(tumpus 'x) -> (small 'x)]]. 4- Each [[prop:impus]] is a [[prop:tumpus]]. [[axiom:(impus 'x) -> (tumpus 'x)]]. 5- Each [[prop:rompus]] is a [[prop:jompus]]. [[axiom:(rompus 'x) -> (jompus 'x)]]. 6- [[prop:tumpus]] are [[prop:wumpus]]. [[axiom:(tumpus 'x) -> (wumpus 'x)]]. 7- Every [[prop:yumpus]] is [[prop:transparent]]. [[axiom:(yumpus 'x) -> (transparent 'x)]]. 8- [[prop:yumpus]] are [[prop:numpus]]. [[axiom:(yumpus 'x) -> (numpus 'x)]]. 9- [[prop:zumpus]] are [[prop:orange]]. [[axiom:(zumpus 'x) -> (orange 'x)]]. 10- [[prop:jompus]] are [[prop:yumpus]]. [[axiom:(jompus 'x) -> (yumpus 'x)]]. 11- [[prop:rompus]] are [[prop:floral]]. [[axiom:(rompus 'x) -> (floral 'x)]]. 12- [[prop:wumpus]] are [[prop:vumpus]]. [[axiom:(wumpus 'x) -> (vumpus 'x)]]. 13- Every [[prop:wumpus]] is [[prop:nervous]]. [[axiom:(wumpus 'x) -> (nervous 'x)]]. 14- Every [[prop:impus]] is [[prop:temperate]]. [[axiom:(impus 'x) -> (temperate 'x)]]. 15- [[prop:jompus]] are not [[prop:sweet]]. [[axiom:(jompus 'x) -> (not (sweet 'x))]]. 16- [[prop:dumpus]] are not [[prop:floral]]. [[axiom:(dumpus 'x) -> (not (floral 'x))]]. 17- Every [[prop:vumpus]] is [[prop:angry]]. [[axiom:(vumpus 'x) -> (angry 'x)]]. 18- [[object:fae]] is a [[prop:vumpus]]. [[axiom:(vumpus fae)]].\nFormalized goal: [[goal:(not (floral fae))]]\nReasoning: [[infer:(angry fae)]] Fae is angry. [[infer:(zumpus fae)]] Fae is a zumpus. [[infer:(orange fae)]] Fae is orange. [[infer:("""

        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained("facebook/opt-13b", use_fast=False)

        vocab = [tokenizer.decode([i]) for i in range(tokenizer.vocab_size)]

        csd = StreamingCSD(ce, vocab)

        tokens = tokenizer.encode(prefix)

        for t in tokens:
            assert csd.can_token_follow(t)
            csd.feed_prediction(t)

        # Only inference possible at this point is '(rompus fae)'
        # We remove the first token because encode with the OPT tokenizer
        # always prepends a 'start of sentence' token.
        assert csd.get_valid_tokens() == tokenizer.encode('rom')[1:]

    def test_closing_token_bug(self):
        d = domain.FirstOrderLogicDomain()
        prob = d.start_derivation()

        ce = PeanoCompletionEngine(d, prob)

        prefix = """Formalized context: 1- [[prop:vumpus]] are [[prop:luminous]].
        [[axiom:(vumpus 'x) -> (luminous 'x)]].
        2- Each [[prop:rompus]] is a [[prop:jompus]]. [[axiom:(rompus 'x) -> (jompus 'x)]].
        3- Every [[prop:jompus]] is not [[prop:luminous]].
           [[axiom:(jompus 'x) -> (not (luminous 'x))"""

        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained("facebook/opt-13b", use_fast=False)

        vocab = [tokenizer.decode([i]) for i in range(tokenizer.vocab_size)]

        csd = StreamingCSD(ce, vocab)

        tokens = tokenizer.encode(prefix)

        for t in tokens:
            assert csd.can_token_follow(t)
            csd.feed_prediction(t)

        valid_tokens = csd.get_valid_tokens()
        # Here, the only valid possibilities are: ' ->' (continuing with an implication)
        # or ]] (finishing the axiom right away).
        assert len(valid_tokens) == 2
        assert tokenizer.encode(' ->')[1] in valid_tokens
        assert tokenizer.encode(']]')[1] in valid_tokens

    def test_proofwriter_prompt_example1(self):
        examples = [("""Formalized context: 1- [[object:anne]] is [[prop:furry]]. [[axiom:(furry anne)]]. 2- [[object:anne]] is not [[prop:young]]. [[axiom:(not (young anne))]]. 3- [[object:gary]] is [[prop:round]]. [[axiom:(round gary)]]. 4- All [[prop:furry]] people are [[prop:round]]. [[axiom:(furry 'x) -> (round 'x)]] 5- [[prop:quiet]] people are not [[prop:furry]]. [[axiom:(quiet 'x) -> (not (furry 'x))]]. 6- [[prop:blue]], [[prop:big]] people are not [[prop:young]]. [[axiom:(blue 'x) -> (big 'x) -> (not (young 'x))]]. 7- If [[object:anne]] is [[prop:round]] then [[object:anne]] is [[prop:blue]]. [[axiom:(round anne) -> (blue anne)]]. 8- If something is [[prop:blue]] and [[prop:round]] then it is not [[prop:big]]. [[axiom:(blue 'x) -> (round 'x) -> (not (big 'x))]]
Formalized goal: [[goal:(big anne)]]
Reasoning: [[infer:(round anne)]] Anne is round. [[infer:(blue anne)]] Anne is blue. [[infer:(not (big anne))]] Anne is not big. This contradicts the goal.""", False),
                    ("""
Context: 1- The lion does not like the mouse. 2- The mouse eats the lion. 3- If someone likes the lion and the lion is rough then they see the mouse. 4- If someone eats the lion then they see the mouse. 5- If someone is rough and they eat the mouse then they are not blue. 6- If someone sees the mouse then they like the lion. 7- If someone is blue and they do not eat the lion then they are rough. 8- If someone likes the lion then the lion is red.
Query: True or false: The lion is red.
Formalized context: 1- The [[object:lion]] does not [[relation:likes]] the [[object:mouse]]. [[axiom:(not (likes lion mouse))]]. 2- The [[object:mouse]] [[relation:eats]] the [[object:lion]]. [[axiom:(eats mouse lion)]]. 3- If someone [[relation:likes]] the [[object:lion]] and the [[object:lion]] is [[prop:rough]] then they [[relation:sees]] the [[object:mouse]]. [[axiom:(likes 'x lion) -> (rough lion) -> (sees 'x mouse)]]. 4- If someone [[relation:eats]] the [[object:lion]] then they [[relation:sees]] the [[object:mouse]]. [[axiom:(eats 'x lion) -> (sees 'x mouse)]]. 5- If someone is [[prop:rough]] and they [[relation:eats]] the [[object:mouse]] then they are not [[prop:blue]]. [[axiom:(rough 'x) -> (eats 'x mouse) -> (not (blue 'x))]]. 6- If someone [[relation:sees]] the [[object:mouse]] then they [[relation:likes]] the [[object:lion]]. [[axiom:(sees 'x mouse) -> (likes 'x lion)]]. 7- If someone is [[prop:blue]] and they do not [[relation:eats]] the [[object:lion]] then they are [[prop:rough]]. [[axiom:(blue 'x) -> (not (eats 'x lion)) -> (rough 'x)]]. 8- If someone [[relation:likes]] the [[object:lion]] then the [[object:lion]] is [[prop:red]]. [[axiom:(likes 'x lion) -> (red lion)]].
Formalized goal: [[goal:(red lion)]]
Reasoning: [[infer:(sees mouse mouse)]] The mouse sees the mouse. [[infer:(likes mouse lion)]] The mouse likes the lion. [[infer:(red lion)]] The lion is red. This was the goal.
Answer: True""", True)]

        for solution, answer in examples:
            d = domain.FirstOrderLogicDomain()
            prob = d.start_derivation()

            ce = PeanoCompletionEngine(d, prob)
            from transformers import AutoTokenizer
            tokenizer = AutoTokenizer.from_pretrained("facebook/opt-13b", use_fast=False)

            vocab = [tokenizer.decode([i]) for i in range(tokenizer.vocab_size)]

            csd = StreamingCSD(ce, vocab)

            tokens = tokenizer.encode(solution)

            for i, t in enumerate(tokens):
                if not csd.can_token_follow(t):
                    breakpoint()
                csd.feed_prediction(t)

            done, prediction = ce.is_complete(solution)

            self.assertTrue(done)
            self.assertEqual(prediction, answer)


    def test_deontic_prompt_example(self):
        solution = """Formalized context: 1- [[object:alice]] [[object:bob]] [[object:carol]] 2- [[object:fundraising]] [[object:training]] [[object:charity-gala]] 3- [[axiom:(organizer alice fundraising)]] [[axiom:(participant carol fundraising)]] 4- [[axiom:(conference fundraising)]] [[axiom:(social charity-gala)]] 5- [[axiom:(monthly training)]] 6- [[axiom:(individual-invite alice fundraising)]] 7- [[axiom:(free 'p 'e) -> (obligatory (check-availability 'p 'e))]] 8- [[axiom:(participant 'p 'e) -> (permissible (delegate 'e 'p))]] 9- [[axiom:(social 'e) -> (public 'e)]] 10- [[axiom:(free 'p 'e) -> (obligatory (suggest-alternative-time 'p 'e))]] 11- [[axiom:(organizer 'p 'e) -> (not (permissible (suggest-alternative-time 'p 'e)))]] 12- [[axiom:(long 'e) -> (permissible (change-visibility 'e confidential))]] 13- [[axiom:(public 'e) -> (long 'e)]] 14- [[axiom:(long 'e) -> (free carol 'e)]] 15- [[axiom:(free 'p 'e) -> (low-priority 'p 'e)]]
        Formalized goal: [[goal:(obligatory (suggest-alternative-time carol charity-gala))]]
        Reasoning: [[infer:(public charity-gala)]] The charity gala is a public event. [[infer:(long charity-gala)]] The charity gala is a long event. [[infer:(free carol charity-gala)]] Carol is free during the charity gala. [[infer:(obligatory (suggest-alternative-time carol charity-gala))]] – It is obligatory for Carol (b3) to suggest an alternative time for the charity gala.
        Answer (Yes or no): Yes, it is obligatory for Carol to suggest an alternative time for the charity gala.
        """

        d = domain.FirstOrderLogicDomain()
        prob = d.start_derivation()

        ce = PeanoCompletionEngine(d, prob)
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained("facebook/opt-13b", use_fast=False)

        vocab = [tokenizer.decode([i]) for i in range(tokenizer.vocab_size)]

        csd = StreamingCSD(ce, vocab)

        tokens = tokenizer.encode(solution)

        for i, t in enumerate(tokens):
            if not csd.can_token_follow(t):
                breakpoint()
            csd.feed_prediction(t)

        done, prediction = ce.is_complete(solution)

        self.assertTrue(done)
        self.assertTrue(prediction)
