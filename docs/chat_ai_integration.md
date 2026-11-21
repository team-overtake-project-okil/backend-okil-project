# Chat WebSocket → AI service (#74, #86)

The adapter connects the `/ws/chat` endpoint from PR #67 to the classifier,
`GroqClient` and `answer_intent` from PR #69. It does not reimplement the model,
change scoring or add a chat REST endpoint. Issue #86 adds an optional
`nextPrompt` to completed events and stored history turns.

## Dependencies and setup

PR #69 (`feat/add-ai_model-validation`) supplies `app.ai_assistant`,
`app.intent_classifier`, model dependencies and database context builders.
At implementation time, its reviewed head was
`0202119c576f059328b161dbc085f387d0ca728b` and it was not merged into main.
For the assembled local project, the integration initially included copies of
`app/ai_assistant.py` and `app/intent_classifier.py` from that exact revision.
Issue #86 extends `GroqClient` with optional next-prompt generation and preserves
the classifier interface and knowledge configuration.
The current classifier descriptions and their scenario encoding are documented in
[intent_classification.md](intent_classification.md). The current main also has
the `ai_knowledge` vector model; curated text export is documented in
[ai_knowledge_ingestion.md](ai_knowledge_ingestion.md). Project-info answers now use up to three indexed knowledge fragments.
The chat reuses the classifier's query embedding for retrieval.
Issue #97 uses 1024-dimensional BGE-M3 embeddings via Cloudflare Workers AI.
See [embedding setup and database migration](embedding_setup.md).

With the dependencies available, install the combined requirements, configure
the existing DB and Redis, and add a Groq key to the backend environment:

```dotenv
GROQ_API_KEY=your_local_key
GROQ_MODEL=openai/gpt-oss-20b
CLOUDFLARE_ACCOUNT_ID=your_account_id
CLOUDFLARE_API_TOKEN=your_workers_ai_token
```

Keep actual keys out of Git and logs. Redis/session settings remain unchanged.
The classifier is initialized lazily once per process and reused. A failed
initialization can be retried. In API mode, batched requests encode all
intent scenarios; no local model is loaded. `CHAT_PRELOAD_AI=true` moves those
requests to startup. It does not call Groq or write to the database. A failed
preload leaves non-chat endpoints available, and a later chat request may retry.
Historical verification sections below describe the original local-model setup.

The actual assistant UI is now merged in frontend main via PR #105. The
frontend integration branch starts at `ef79e0b` and uses:

```dotenv
VITE_CHAT_WS_URL=ws://localhost:8000/ws/chat
```

The existing main-project map mounts the assistant. An explicit WS URL is
optional: the frontend can derive it from VITE_BACKEND_URL. The WebSocket
requires the frontend Origin in CORS_ORIGINS. The popup/layout is preserved.

## Message flow and supported context

The client still sends `session.open`, waits for `session.ready`, then sends:

```json
{"type":"message.send","requestId":"unique-id","text":"Що таке OKIL?"}
```

The event sequence remains `message.accepted`, `answer.status` with stage
`generating`, then `answer.completed` containing `text`, or an existing-format
`error` event with `requestId`, `code` and readable `message`.

1. `ChatSessionStore.begin` claims the session and supplies completed history.
2. The upstream classifier selects supported question parts. Only those parts
   are passed to `answer_intent`; unrelated parts are not forwarded to Groq.
3. Project questions use the upstream project's PostgreSQL context. POI and
   score questions additionally require a valid resolution-9 H3 ID.
4. For H3, the adapter first checks the current question, then previous user
   questions from newest to oldest. It never extracts context from AI answers.
   Ambiguous or invalid explicit context is rejected rather than silently
   falling back to another cell. Example:

   ```json
   {"type":"message.send","requestId":"hex-1","text":"Які місця в 891e7689e8fffff?"}
   ```

   A later `Які тут місця?` can reuse that H3 from completed Redis history.
5. `answer_intent` builds factual DB context and returns the upstream answer.
6. Optional next-prompt generation enriches the answer within the remaining
   reply budget. The answer and any valid suggestion are saved together before
   `answer.completed` is sent.

The current UI prefixes text with the selected H3, displayed score and
category scores. The adapter removes that specific header before classifying
the question and uses only the validated H3; factual scores are re-read from
PostgreSQL, not trusted from client text. Explicit project FAQs and requests
to explain a hex score receive narrow routing corrections for observed
classifier confusion. Other parts continue through the upstream classifier.
Custom user weights are not passed to answer_intent, so AI score context uses
server defaults. Main-answer generation does not accept a general transcript:
Redis preserves history, and the main-answer adapter reuses only supported H3
context. Suggestion generation uses the limited context described below.

## Contextual next-prompts (#86)

After a successful main answer, `generate_next_prompt` asks the existing Groq
client for one optional follow-up question, phrased as a question the user can
send to the assistant. It uses the same key and configured model. There is one
additional provider request per eligible new answer; duplicate request IDs reuse
the saved result without either provider request.

The suggestion request includes the current question (without the map's score
header), up to 8,000 characters of the main answer, a validated H3 ID when
available, the last three user questions and the last three saved suggestions.
Session tokens and request IDs are not included. H3 context comes only from user
questions, never from model output. Unlike the main-answer classifier path, the
suggestion request includes the whole current question; its prompt treats this
as context, not as instructions.

The prompt limits suggestions to supported OKIL topics, requires H3 context for
questions about a specific cell, and requests `null` when a follow-up would be
irrelevant or the available information is insufficient. The unrelated-question
reply skips suggestion generation entirely. The model returns internal JSON;
the WebSocket exposes only the extracted plain string:

```json
{"type":"answer.completed","requestId":"req_1","text":"<main answer>","nextPrompt":"Які категорії оцінює OKIL?"}
```

Suggestions longer than 240 characters, empty values, non-string values,
multiline text, markup and links are rejected. Exact repeats of the current or
saved questions/suggestions are rejected after case, punctuation and whitespace
normalization. Semantic relevance and paraphrase avoidance rely on the model.
When omitted, `nextPrompt` is absent rather than an empty string or JSON object.

Before returning a suggestion, the adapter checks it with the same classifier
and routing corrections used for normal questions. It omits unrecognized
questions, POI/score questions without H3 context, and model-invented H3 IDs.
General questions such as `Які категорії оцінює OKIL?` and
`Які критерії ... в OKIL?` use the project-information service rather than
requiring a selected cell. This correction excludes questions referring to a
specific cell or asking for places. The classifier model and supported intent
types are unchanged.

The optional step has a three-second timeout, further limited by whatever
remains of the existing 30-second generation budget. A timeout, provider error,
invalid JSON or rejected suggestion returns the ready main answer without the
field. No extra retry or fallback model is used. Disconnect cancellation still
propagates and releases the session claim.

History stores the same optional `nextPrompt`, so reconnects and duplicate
requests return the original suggestion. Old Redis turns without that field
continue to work without migration. The existing frontend can ignore the new
field; rendering a suggestion is outside this backend task.

### Compatibility with session clearing (#83)

The existing `session.clear` event removes the entire current Redis session,
including every saved `nextPrompt`. The client must open a new session before
sending another question. Request IDs in the new session do not replay the
deleted answers or suggestions, and generation receives an empty history.

Clearing a session while its optional suggestion is being generated cancels
that socket's provider task. If another socket clears the same session, a late
answer or suggestion cannot recreate the deleted history: saving it fails with
`session_expired`. The clear-event token validation and error envelope are
unchanged.

## Errors and session guarantees

- `ai_unavailable`: dependency, model or provider configuration cannot initialize.
- `ai_failed`: provider/DB/classifier failure or malformed/empty answer.
- `context_required`: POI/score question needs an explicit H3 ID.
- `invalid_context`: invalid, ambiguous or non-resolution-9 H3 context.
- Existing `reply_timeout`, `busy`, session, protocol and storage errors remain.

Provider response bodies, credentials and SQL details are not sent to clients.
Failure logs contain exception class names only. The socket stays open after
request errors. Failed/timed-out replies are not committed to history; the
claim is released so the same request can be retried. Completed duplicate
request IDs reuse the saved answer without another AI call. Disconnect still
cancels the task. The 30-second timeout, 30-turn history and Redis TTL/locking
implementation are unchanged.

Before calling the AI service, chat probes its DB session with `SELECT 1`.
If opening/probing the session fails with a SQLAlchemy `DBAPIError` marked
`connection_invalidated`, it closes that session and reconnects once. This
handles connections closed by the remote DB while idle. Other DB errors are
not retried. The retry block ends before `answer_intent`, so an AI invocation
or later failure never causes a duplicate provider call. The shared engine's
settings and other endpoints remain unchanged.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pip install -r tests/requirements-chat.txt
.\.venv\Scripts\python.exe -m pytest tests/test_ai_assistant.py tests/test_chat_service.py tests/test_chat_websocket.py tests/test_chat_startup.py -q
```

The WebSocket tests use a Redis-compatible in-process fake and real session
store operations; no shared Redis or PostgreSQL data is touched. Unit tests
exercise the adapter contract without downloading models or using credentials.
Next-prompt checks cover contextual output, omission, repeated/malformed output,
provider failure, deadline exhaustion, persistence, reconnect and duplicate
requests. These checks mock Groq and do not measure live suggestion quality.

### Merge verification with current main (2026-11-17)

After integrating main's session clearing and classifier/knowledge updates, the
full backend suite passes 447 tests. Both the original next-prompt tests and
the session-clearing tests are retained. Additional integration tests verify
that clearing removes saved suggestions, cancels an in-flight suggestion, and
prevents a suggestion completing on another socket from restoring deleted
history. These checks use the in-process Redis fixture and controlled providers.

### Live verification of #86 (2026-11-15)

The final changes pass 110 AI/chat tests, including regressions for generated
project questions being misclassified as cell-score requests and for omitting
suggestions that cannot be processed with the available context.

A separate live check used the actual `main:app` over a localhost WebSocket,
Groq `openai/gpt-oss-20b`, the real multilingual Sentence Transformers classifier,
Redis 7.0.15 in WSL and PostgreSQL. Database sessions were explicitly read-only;
startup schema initialization was disabled. Only the test's own anonymous
Redis session was removed afterward.

- A project question returned an answer and a plain-text follow-up about the
  health category's criteria. Sending that generated question back to the chat
  succeeded without an H3 ID and returned the actual catalog radii.
- A score question for `891e7689e8fffff` returned its main answer with no
  suggestion, exercising the optional-field path.
- Reconnecting restored all three completed turns, including their suggestions.
- Replaying the original request returned the identical completed event, with
  no extra main-answer or suggestion provider calls.
- Separate direct Groq checks returned contextual suggestions in under
  1.4 seconds and `null` for a user who explicitly ended the conversation.

This verifies the backend protocol and real providers; frontend rendering is
outside #86. Failure, timeout and disconnect cases are covered by automated
tests with controlled failures, rather than by disrupting the live providers.

The local `.env` uses the following non-secret startup settings:

```dotenv
DB_INIT_ON_STARTUP=false
CHAT_PRELOAD_AI=true
REDIS_URL=redis://127.0.0.1:6379/0
```

For an existing Ubuntu/WSL Redis installation, start it with
`wsl -d Ubuntu -u root -- service redis-server start`. The first model download
and load took about 38 seconds, so preload keeps this outside the reply budget.
Install `requirements.txt` into the same environment used to run Uvicorn;
`websockets` is required. The current embedding setup uses Cloudflare Workers AI.

Controlled integration also used the unchanged service/classifier modules from
PR #69 and the unchanged React `Chat.tsx` from PR #95. The actual component was
mounted in a DOM harness and connected over a real localhost WebSocket to
Uvicorn. Redis was simulated; embeddings, DB rows and Groq HTTP responses were
controlled fixtures. Checks covered an answer, session restoration, a readable
provider error, missing H3, explicit H3 and a follow-up using history.

The initial controlled check proves the component/protocol/service connection
with fixtures, not a live provider or real Redis deployment.

On 2026-11-11 a separate live check loaded the actual 384-dimensional
`paraphrase-multilingual-MiniLM-L12-v2` classifier, used real PostgreSQL facts
inside verified `READ ONLY` transactions, and called Groq's configured
`openai/gpt-oss-20b` model over HTTPS. Three questions (project information,
POI, hex score) reached the unchanged frontend component as `answer.completed`;
the history also restored on remount. The tested cell was
`891e7689e8fffff`: the POI answer named the actual psychiatric hospital and the
score answer returned 79. The key remains in the ignored local `.env`.

Redis was still simulated in this live check, and the React DOM harness does
not replace visual browser acceptance or testing a deployed frontend with real
Redis. At that earlier stage, PR #69/#95 modules were used unchanged in an
isolated checkout. The actual-project assembly below supersedes that setup.

Observed AI-quality limitation: the score answer described the formula as a
weighted sum, whereas `app.scoring.calculate_overall_rating` uses a weighted
geometric mean. The numeric score was correct, but the explanation is not
fully accurate. Record this for the AI-service owner; the adapter does not
change scoring or silently rewrite the provider's answer.

## Actual-project local run

Run real Redis on 6379, `python -m uvicorn main:app --reload --port 8000`
from this backend, and Vite on 5173 from frontend branch
`feat/74-chat-ai-integration`. Open the map and **Помічник Окіл** in the
actual OKIL page. The separate 8765/8766 harness is not used for this flow.

For testing against the existing shared database, local `.env` uses
`DB_INIT_ON_STARTUP=false`; startup DDL is skipped. The setting defaults to
true to preserve the existing initialization behavior for other environments.
AI queries only read data; no imports or rating recalculations are run.

The final browser check used the actual frontend from PR #105, actual backend,
real Redis 7.4.11, real database facts and Groq. The assistant answered the
project question, named the psychiatric hospital at the tested H3, and returned
the default score 79. History restored after both servers restarted while
Redis stayed running. A cold first request initially timed out; the local
startup preload setting was added and verified separately. Numeric/context
integration passes; formula wording remains the AI quality limitation above.

Source/config backups before assembly are under the workspace
`work/chat74-assembly-backup`, outside both repositories. The backup excludes
secrets and records previous local URL/settings for a scoped rollback. No
commits or pushes were made.
