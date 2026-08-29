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


@dataclass(frozen=True)
class UnusualSignal:
    post_id: int
    date: datetime
    location: str
    quote: str
    action_code: str
    severity: int
    reason_code: str
    baseline_messages: int
    current_messages: int
    novelty_score: float


ACTION_FEATURES = {
    'ROUTINE_CHECK': (1, ('стоят', 'проверяют документ', 'проверка документов')),
    'HOME_VISIT': (4, ('по домам', 'по квартирам', 'дергают ручки', 'стучат в двери')),
    'FORCE': (5, ('силой', 'скрутили', 'тащат', 'запихнули', 'избили')),
    'DETENTION': (4, ('задержали', 'увезли', 'забрали человека')),
    'MASS_CHECK': (4, ('массовая проверка', 'много экипажей', 'несколько бус')),
    'PURSUIT': (3, ('догоняют', 'бегают за', 'преследуют')),
}


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


def _normalize(value: str) -> str:
    return re.sub(r'\s+', ' ', value.casefold()).strip()


def _matched_actions(text: str) -> tuple[str, ...]:
    normalized = _normalize(text)
    return tuple(
        action_code
        for action_code, (_, patterns) in ACTION_FEATURES.items()
        if any(pattern in normalized for pattern in patterns)
    )


def find_unusual_signals(
    report_posts: Iterable[NoveltyPost],
    baseline_posts: Iterable[NoveltyPost],
    limit: int = 3,
) -> list[UnusualSignal]:
    report_posts = list(report_posts)
    baseline_posts = list(baseline_posts)
    current_action_ids: dict[str, set[int]] = {}
    baseline_action_ids: dict[str, set[int]] = {}
    baseline_location_ids: dict[tuple[str, str], set[int]] = {}

    for post in report_posts:
        for action_code in _matched_actions(post.text):
            current_action_ids.setdefault(action_code, set()).add(post.id)

    for post in baseline_posts:
        locations = post.locations or ('',)
        for action_code in _matched_actions(post.text):
            baseline_action_ids.setdefault(action_code, set()).add(post.id)
            for location in locations:
                location_key = _normalize(location)
                baseline_location_ids.setdefault((action_code, location_key), set()).add(post.id)

    ranked: list[tuple[int, int, int, int, str, str, UnusualSignal]] = []
    seen: set[tuple[int, str, str]] = set()
    for post in report_posts:
        locations = post.locations or ('',)
        for action_code in _matched_actions(post.text):
            severity = ACTION_FEATURES[action_code][0]
            baseline_messages = len(baseline_action_ids.get(action_code, ()))
            current_messages = len(current_action_ids[action_code])
            weekly_baseline = baseline_messages / 12.85
            novelty_score = current_messages / max(1.0, weekly_baseline)

            for location in locations:
                location = re.sub(r'\s+', ' ', location).strip()
                location_key = _normalize(location)
                dedupe_key = (post.id, action_code, location_key)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)

                location_messages = len(
                    baseline_location_ids.get((action_code, location_key), ())
                )
                if severity >= 4 and baseline_messages <= 1:
                    reason_code = 'HIGH_SEVERITY_NEW'
                elif location_messages == 0 and baseline_messages >= 3:
                    reason_code = 'NEW_LOCATION_ACTION'
                elif current_messages >= 3 and current_messages >= 2 * max(1.0, weekly_baseline):
                    reason_code = 'FREQUENCY_SPIKE'
                elif action_code != 'ROUTINE_CHECK' and baseline_messages == 0:
                    reason_code = 'RARE_ACTION'
                else:
                    continue

                signal = UnusualSignal(
                    post_id=post.id,
                    date=post.date,
                    location=location,
                    quote=re.sub(r'\s+', ' ', post.text).strip()[:180],
                    action_code=action_code,
                    severity=severity,
                    reason_code=reason_code,
                    baseline_messages=baseline_messages,
                    current_messages=current_messages,
                    novelty_score=float(novelty_score),
                )
                ranked.append((
                    -severity,
                    baseline_messages,
                    -current_messages,
                    post.id,
                    action_code,
                    location_key,
                    signal,
                ))

    ranked.sort(key=lambda item: item[:-1])
    return [item[-1] for item in ranked[:max(0, limit)]]
