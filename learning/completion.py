#!/usr/bin/env python3

import copy
import regex

from typing import Optional

import unittest
import domain


class CountingDomain:
    def __init__(self, start, step, end):
        self.start = start
        self.step = step
        self.end = end

    def actions(self, blocks):
        return {str(self.start + len(blocks) * self.step)}

    def is_complete(self, blocks: list[str]) -> bool:
        return len(blocks) > 0 and int(blocks[-1]) >= self.end


def regex_not_containing(m):
    'Returns a regular expression for any string that does not contain m.'
    options = []

    for i in range(len(m)):
        options.append(f'{regex.escape(m[:i])}[^{regex.escape(m[i])}]')
    return f'({"|".join(options)})*'


def _split_block(b: str) -> (str, str):
    colon = b.index(':')
    return (b[:colon], b[colon+1:])


class PeanoCompletionEngine:
    '''CSD completion engine backed by a Peano domain.'''
    def __init__(self, domain, start_derivation,
                 format_fn=lambda s: s, start_marker='[[', end_marker=']]'):
        self.domain = domain
        self.start_derivation = start_derivation
        self.start_marker = start_marker
        self.end_marker = end_marker
        self.format_fn = format_fn

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
            block_keywords = f'(prop|object|axiom|goal|infer):'
            return regex.compile(block_keywords)

        block_keyword, block_contents = _split_block(b)
        assert not block_contents

        if block_keyword in ('prop', 'object'):
            return regex.compile('[a-z_]+' + end_marker)

        if block_keyword in ('axiom', 'goal'):
            # NOTE: We can plug in a grammar for axioms here,
            # though for now we just trust the model in this one.
            return regex.compile(regex_not_containing(self.end_marker) +
                                 end_marker)

        if block_keyword == 'infer':
            # Match any of the actions followed by the end marker.
            verified_blocks = self.get_verified_blocks(prefix)
            ff_derivation = self.fast_forward_derivation(verified_blocks)
            choices = self.enumerate_choices(ff_derivation.universe)

            # Filter duplicate inferences.
            new_choices = []
            for c in choices:
                inference = self.format_fn(self.domain.value_of(ff_derivation.universe, c))
                is_new = True

                for keyword, content in verified_blocks:
                    if keyword == 'infer' and content == inference:
                        is_new = False
                        break

                if is_new:
                    new_choices.append(inference)

            out = '|'.join(map(regex.escape, new_choices))

            return regex.compile(f'({out}){end_marker}')

        raise ValueError(f'Invalid block type {block_keyword}.')


    def fast_forward_derivation(self, verified_blocks: list[tuple[str, str]]):
        u = self.start_derivation.universe.clone()

        u.incorporate('object : type. not : [prop -> prop].')
        goal = None

        for i, (block_type, block_content) in enumerate(verified_blocks):
            if block_type == 'prop':
                u.incorporate(f'{block_content} : [object -> prop].')
            elif block_type == 'axiom':
                # Wrap arrow types.
                if block_content.find('->') != -1:
                    block_content = f'[{block_content}]'

                u.incorporate(f'axiom{i} : {block_content}.')
            elif block_type == 'object':
                u.incorporate(f'let {block_content} : object.')
            elif block_type == 'goal':
                goal = block_content
            elif block_type == 'infer':
                choices = self.enumerate_choices(u)

                found = False
                for c in choices:
                    if self.format_fn(self.domain.value_of(u, c)) == block_content:
                        # Found the choice made at this step.
                        found = True
                        self.domain.define(u, f'!step{i}', c)
                        break

                assert found, 'Could not replay inference in verified block.'

        d_prime = copy.copy(self.start_derivation)
        d_prime.universe = u
        d_prime.goal = goal

        return d_prime


    def enumerate_choices(self, universe):
        initial_actions = set(self.domain.derivation_actions(self.start_derivation.universe) +
                              self.domain.tactic_actions())
        arrows = self.domain.derivation_actions(universe)

        choices = []

        for a in arrows:
            if a in initial_actions or a.startswith('axiom'):
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
        ff = self.fast_forward_derivation(blocks)
        return self.domain.derivation_done(ff)


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

        self.assertTrue(completions.match('(tumpus sally)]]'))
        self.assertFalse(completions.match('(rompus sally)]]'))

        p2 = '''
1- Vumpuses are zumpuses. 2- Each zumpus is a rompus. 3- Every tumpus is small. 4- Each impus is a tumpus. 5- Each rompus is a jompus. 6- Tumpuses are wumpuses. 7- Every yumpus is transparent. 8- Yumpuses are numpuses. 9- Zumpuses are orange. 10- Jompuses are yumpuses. 11- Rompuses are floral. 12- Wumpuses are vumpuses. 13- Every wumpus is nervous. 14- Every impus is temperate. 15- Jompuses are not sweet. 16- Dumpuses are not floral. 17- Every vumpus is angry. 18- Sally is a tumpus.
Query: True or false: Sally is not floral.
Formalized context: 1- [[prop:vumpus]] are [[prop:zumpus]]. [[axiom:(vumpus 'x) -> (zumpus 'x)]]. 2- Each [[prop:zumpus]] is a [[prop:rompus]]. [[axiom:(zumpus 'x) -> (rompus 'x)]]. 3- Every [[prop:tumpus]] is [[prop:small]]. [[axiom:(tumpus 'x) -> (small 'x)]]. 4- Each [[prop:impus]] is a [[prop:tumpus]]. [[axiom:(impus 'x) -> (tumpus 'x)]]. 5- Each [[prop:rompus]] is a [[prop:jompus]]. [[axiom:(rompus 'x) -> (jompus 'x)]]. 6- [[prop:tumpus]] are [[prop:wumpus]]. [[axiom:(tumpus 'x) -> (wumpus 'x)]]. 7- Every [[prop:yumpus]] is [[prop:transparent]]. [[axiom:(yumpus 'x) -> (transparent 'x)]]. 8- [[prop:yumpus]] are [[prop:numpus]]. [[axiom:(yumpus 'x) -> (numpus 'x)]]. 9- [[prop:zumpus]] are [[prop:orange]]. [[axiom:(zumpus 'x) -> (orange 'x)]]. 10- [[prop:jompus]] are [[prop:yumpus]]. [[axiom:(jompus 'x) -> (yumpus 'x)]]. 11- [[prop:rompus]] are [[prop:floral]]. [[axiom:(rompus 'x) -> (floral 'x)]]. 12- [[prop:wumpus]] are [[prop:vumpus]]. [[axiom:(wumpus 'x) -> (vumpus 'x)]]. 13- Every [[prop:wumpus]] is [[prop:nervous]]. [[axiom:(wumpus 'x) -> (nervous 'x)]]. 14- Every [[prop:impus]] is [[prop:temperate]]. [[axiom:(impus 'x) -> (temperate 'x)]]. 15- [[prop:jompus]] are not [[prop:sweet]]. [[axiom:(jompus 'x) -> (not (sweet 'x))]]. 16- [[prop:dumpus]] are not [[prop:floral]]. [[axiom:(dumpus 'x) -> (not (floral 'x))]]. 17- Every [[prop:vumpus]] is [[prop:angry]]. [[axiom:(vumpus 'x) -> (angry 'x)]]. 18- [[object:sally]] is a [[prop:tumpus]]. [[axiom:(tumpus sally)]].
Formalized goal: [[goal:(not (floral sally))]]
Reasoning: [[infer:(tumpus sally)]] Sally is a tumpus. [[infer:(wumpus sally)]] Sally is a wumpus.
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