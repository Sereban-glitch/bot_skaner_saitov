import unittest
from datetime import datetime, timezone

from report_novelty import NoveltyPost, find_slang_candidates


class SlangDetectionTests(unittest.TestCase):
    def post(self, post_id, day, text):
        return NoveltyPost(post_id, day, text, (), ('check',))

    def test_requires_five_distinct_messages(self):
        now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
        recent = [self.post(i, now, 'заметили смурфики возле рынка') for i in range(1, 5)]
        self.assertEqual(find_slang_candidates(recent, [], set()), [])

    def test_repeated_word_in_one_message_counts_once(self):
        now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
        recent = [
            self.post(1, now, 'смурфики смурфики смурфики'),
            *[self.post(i, now, 'видели смурфики') for i in range(2, 6)],
        ]
        result = find_slang_candidates(recent, [], set())
        self.assertEqual(result[0].recent_messages, 5)

    def test_known_or_stable_word_is_not_candidate(self):
        now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
        old = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
        recent = [self.post(i, now, 'обычная проверка маслины') for i in range(1, 7)]
        baseline = [self.post(i + 100, old, 'обычная проверка маслины') for i in range(28)]
        self.assertEqual(find_slang_candidates(recent, baseline, {'проверка'}), [])

    def test_new_fast_growing_word_is_candidate(self):
        now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
        recent = [self.post(i, now, 'появились смурфики') for i in range(1, 7)]
        result = find_slang_candidates(recent, [], {'появились'})
        self.assertEqual(result[0].token, 'смурфики')
