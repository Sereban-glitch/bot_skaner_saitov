# Report Anomaly Detection Design

## Goal

Extend the daily Telegram report with two capabilities:

1. Move AI proxy configuration out of Python and into environment settings.
2. Detect emerging slang and unusual events related only to document checks, police, and TCC activity.

The report must remain retrospective, evidence-based, idempotent, and safe when AI is unavailable.

## Scope

Included:

- Previous completed Kyiv day only for the daily anomaly section.
- A 90-day baseline excluding the report day.
- Events related to checks, police, military/TCC activity, detention, force, home visits, mass checks, and changed operating patterns.
- Single-message unusual signals, explicitly labeled as unconfirmed public reports.
- Emerging unknown terms that appear in at least five relevant messages during the latest seven days and grow materially against the preceding 28-day baseline.

Excluded:

- General city emergencies, accidents, fires, crime, missing persons, and unrelated shelling reports.
- Claims that an event is confirmed.
- Route advice or guarantees of safe places and times.
- A separate monthly timer or report.

## Configuration

The report reads these settings from the existing `.env` file:

- `REPORT_AI_URL`: Anthropic-compatible messages endpoint.
- `REPORT_AI_KEY`: proxy API key.
- `REPORT_AI_MODEL`: configured model name.

The current production values are migrated without printing them. If any required AI setting is missing, the AI step is skipped and deterministic reporting continues.

## Processing Pipeline

### 1. Input cleaning

Reuse the current cleaning and filtering before any new analysis:

- remove channel boilerplate;
- remove URLs and user handles from token analysis;
- preserve the original cleaned text for evidence quotes;
- count each Telegram message once regardless of repeated terms inside it.

### 2. Relevant-message gate

Anomaly and slang analysis only receives messages already classified as related to checks, police, or TCC activity. Unrelated channel posts cannot create candidates.

### 3. Baseline

Use source messages from the 90 calendar days before the report day. The report day is never included in its own baseline.

For each normalized feature, retain:

- number of distinct messages;
- number of active days;
- latest occurrence date;
- representative source excerpts;
- combinations with recognized location and event type.

No additive persistent counter is required. Recomputing from Telegram source keeps preview and repeated runs idempotent.

## Emerging Slang

Tokenization considers Cyrillic words of at least five letters after normalization.

Exclude:

- stopwords and ordinary service vocabulary;
- known event terms and their configured aliases;
- known location aliases;
- URLs, handles, numbers, and channel boilerplate;
- words that occur broadly across unrelated vocabulary in the 90-day baseline.

A token is shown only when all conditions hold:

- it appears in at least five distinct relevant messages in the latest seven days;
- its seven-day message rate is at least twice the average seven-day rate during the preceding 28 days, or it was absent in that baseline;
- it is not already present in configured dictionaries.

Show at most three candidates, ordered by distinct-message count and growth. Each item contains the token, message count, and one shortened source context.

## Unusual Signals

### Candidate generation

Python generates candidates from four deterministic novelty signals:

1. A rare action phrase compared with the 90-day baseline.
2. A rare combination of event type and recognized location.
3. A sudden frequency increase for an otherwise known action.
4. A new high-severity action marker, such as force, detention, home visits, or a mass check, that is absent or rare in the baseline.

Candidate generation may accept one source message. Corroboration is not required by product decision.

### Ranking

An internal novelty score ranks candidates but is never displayed as an unexplained number. The score combines:

- baseline rarity;
- current frequency increase;
- action severity category;
- specificity of the recognized location;
- independence of source message IDs.

At most three unusual signals are shown.

### AI role

AI receives only Python-generated candidates, delimited as untrusted data. It may produce one short explanation of why a candidate differs from the baseline.

AI cannot introduce a candidate, quote, time, location, event type, or numeric claim. Before publication:

- the quote must match one exact source-message boundary;
- the location must match deterministic contextual extraction from that message;
- the event type must be allowlisted;
- the explanation must not contain unsupported locations, times, counts, or certainty claims.

If validation fails, use a deterministic explanation such as `rare action compared with the previous 90 days`.

## Report Format

Keep the existing two-message report.

Message 1 remains the dashboard and is unchanged by this feature.

Message 2 keeps daily points and seven-day repeated places, then conditionally adds:

```text
⚠️ Необычные сигналы
14:20 — Хортицкое шоссе
Почему выделено: действие почти не встречалось за предыдущие 90 дней.
Одно публичное сообщение, не подтверждение.
«Точная цитата»

🆕 Возможный новый сленг
«слово» — 7 сообщений за 7 дней
Контекст: «короткий исходный фрагмент»
```

Omit either section when it has no qualifying items. Preserve deterministic points before optional AI prose when enforcing the Telegram UTF-16 limit.

## Error Handling

- Missing AI settings: skip AI and use deterministic explanations.
- AI timeout or malformed output: skip AI and use deterministic explanations.
- Missing or insufficient baseline: describe the signal as new in available history and avoid percentage claims.
- Empty report day: emit no anomaly or slang sections.
- Repeated run: produce the same candidates and do not accumulate counts.

## Security

- Treat all Telegram text as untrusted data.
- Do not interpolate raw messages into system instructions.
- Bound candidate count, excerpt length, prompt size, response size, and network timeout.
- Never log or display `REPORT_AI_KEY`.
- Do not publish AI-only facts.

## Testing

Use TDD for pure functions before integration.

Required tests:

- AI URL, key, and model are read from environment settings.
- Missing AI settings skip AI without breaking deterministic details.
- A known ordinary check is not an anomaly.
- A single genuinely novel action can be an unusual signal.
- A frequent historical event is not labeled new.
- The report day is excluded from its baseline.
- Repeated terms in one post count as one message.
- Boilerplate and prompt-injection text cannot create candidates.
- Unknown slang requires five distinct relevant messages.
- A stable common word does not qualify; a materially growing unknown word does.
- Candidate evidence is tied to the exact message ID, time, location, and quote.
- Empty AI output uses deterministic fallback.
- Telegram UTF-16 size remains within the limit.
- Preview sends zero messages; production delivery remains exactly two messages.
- Existing previous-day, DST, 24-hour chart, and 7/14/90-day comparison tests remain green.

## Deployment

Implement in the existing isolated worktree. Run unit tests, compile checks, production-config preview without sending, and independent code review. Copy the verified script and tests to production only after explicit approval for the production write. Do not manually trigger the report service.
