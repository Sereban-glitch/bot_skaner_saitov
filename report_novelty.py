import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


WORD_RE = re.compile(r'[а-яё]{5,}', re.IGNORECASE)


@dataclass(frozen=True)
class NoveltyPost:
    id: int
    date: datetime
    text: str
    locations: tuple[str, ...]
    event_types: tuple[str, ...]


@dataclass(frozen=True)
class SlangCandidate:
    token: str
    recent_messages: int
    baseline_weekly_rate: float
    sample_post_id: int
    sample_text: str


def message_tokens(text: str, known_terms: set[str]) -> set[str]:
    cleaned = re.sub(r'https?://\S+|@\w+', ' ', text.lower())
    return {
        token
        for token in WORD_RE.findall(cleaned)
        if token not in known_terms
    }


def find_slang_candidates(
    recent_posts: Iterable[NoveltyPost],
    baseline_posts: Iterable[NoveltyPost],
    known_terms: set[str],
    min_messages: int = 5,
    growth_factor: float = 2.0,
    limit: int = 3,
) -> list[SlangCandidate]:
    recent_ids: dict[str, set[int]] = {}
    baseline_ids: dict[str, set[int]] = {}
    samples: dict[str, tuple[int, str]] = {}

    for post in recent_posts:
        for token in message_tokens(post.text, known_terms):
            recent_ids.setdefault(token, set()).add(post.id)
            samples.setdefault(token, (post.id, post.text))

    for post in baseline_posts:
        for token in message_tokens(post.text, known_terms):
            baseline_ids.setdefault(token, set()).add(post.id)

    ranked: list[tuple[int, float, str, SlangCandidate]] = []
    for token, post_ids in recent_ids.items():
        recent_messages = len(post_ids)
        baseline_weekly_rate = len(baseline_ids.get(token, ())) / 4
        if recent_messages < min_messages:
            continue
        if baseline_weekly_rate and recent_messages < baseline_weekly_rate * growth_factor:
            continue

        sample_post_id, sample_text = samples[token]
        growth_ratio = recent_messages / baseline_weekly_rate if baseline_weekly_rate else float('inf')
        candidate = SlangCandidate(
            token=token,
            recent_messages=recent_messages,
            baseline_weekly_rate=baseline_weekly_rate,
            sample_post_id=sample_post_id,
            sample_text=re.sub(r'\s+', ' ', sample_text).strip()[:180],
        )
        ranked.append((-recent_messages, -growth_ratio, token, candidate))

    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked[:limit]]
