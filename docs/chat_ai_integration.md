# Chat WebSocket → AI service (#74)

The adapter connects the `/ws/chat` endpoint from PR #67 to the classifier,
`GroqClient` and `answer_intent` from PR #69. It does not reimplement the model,
change scoring, add a chat REST endpoint or change Redis session storage.

## Dependencies and setup

PR #69 (`feat/add-ai_model-validation`) supplies `app.ai_assistant`,
`app.intent_classifier`, model dependencies and database context builders.
At implementation time, its reviewed head was
`0202119c576f059328b161dbc085f387d0ca728b` and it was not merged into main.
For the assembled local project, the integration initially included copies of
`app/ai_assistant.py` and `app/intent_classifier.py` from that exact revision.
The current classifier descriptions and their scenario encoding are documented in
[intent_classification.md](intent_classification.md). The current main also has
the `ai_knowledge` vector model; curated text export is documented in
[ai_knowledge_ingestion.md](ai_knowledge_ingestion.md). That export does not yet
add retrieval to `get_project_information`.
`sentence-transformers` is included in requirements. Lazy initialization
still returns `ai_unavailable` when a deployment lacks the model or key.

With the dependencies available, install the combined requirements, configure
the existing DB and Redis, and add a Groq key to the backend environment:

```dotenv
GROQ_API_KEY=your_local_key
GROQ_MODEL=openai/gpt-oss-20b
```

Keep actual keys out of Git and logs. Redis/session settings remain unchanged.
The classifier is initialized lazily once per process and reused. A failed
initialization can be retried. The first load may download the upstream
Sentence Transformers model; a cold start can exceed the existing 30-second
reply deadline. Set `CHAT_PRELOAD_AI=true` to load the classifier during
startup, before the server accepts requests. This optional setting is enabled
in the local assembly and defaults to false elsewhere. It does not call Groq
or write to the database. If initialization fails, other endpoints can still
start and a subsequent chat request may retry. Model files must be cached or
downloadable before startup completes.

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
   Valid nonempty answers are saved before `answer.completed` is sent.

The current UI prefixes text with the selected H3, displayed score and
category scores. The adapter removes that specific header before classifying
the question and uses only the validated H3; factual scores are re-read from
PostgreSQL, not trusted from client text. Explicit project FAQs and requests
to explain a hex score receive narrow routing corrections for observed
classifier confusion. Other parts continue through the upstream classifier.
Custom user weights are not passed to answer_intent, so AI score context uses
server defaults. The upstream API does not accept a general transcript: Redis
preserves history, and the adapter reuses only supported H3 context.

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
.\.venv\Scripts\python.exe -m pytest tests/test_chat_service.py tests/test_chat_websocket.py -q
```

The WebSocket tests use a Redis-compatible in-process fake and real session
store operations; no shared Redis or PostgreSQL data is touched. Unit tests
exercise the adapter contract without downloading models or using credentials.

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
