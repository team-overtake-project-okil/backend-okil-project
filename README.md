# backend-okil-project

After downloading and opening in code redactor

-paste command pip install -r requirements.txt in terminal 

-copy file .env.example and name it .env in that file put url from telegram instead of PUTDATABASEURL

## Frontend access (CORS)

Run the backend from this directory:

```sh
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

The frontend should send requests to `http://localhost:8000`.
By default, browser GET requests are allowed from `http://localhost:5173`.
Set `CORS_ORIGINS` in `.env` to a JSON array of exact frontend origins
(protocol, hostname and port, without a path or trailing slash):

```dotenv
CORS_ORIGINS=["http://localhost:5173","http://127.0.0.1:5173"]
```

Add the deployed frontend origin in the backend deployment environment when
deploying. Restart the backend after changing this setting. CORS enables browser
access; it does not authenticate users or prevent other clients from calling
the API. Cookies and authorization headers are not required for this integration.

To check browser access without database records, open the frontend, then run
this in its browser console while the backend is running:

```js
fetch("http://localhost:8000/openapi.json")
  .then((response) => response.json())
  .then(console.log);
```

This checks connectivity only; application GET endpoints are implemented separately.

## AI knowledge vectors

The `ai_knowledge` table stores text fragments and their 1536-dimensional
embeddings in PostgreSQL with pgvector. It is intended for searchable reference
content about the project. Its `category` column is an optional topic label; it
does not classify user questions. `metadata` is JSONB for optional source and
future fields. Embeddings stored in this table must have 1536 dimensions.

The API enables the PostgreSQL `vector` extension at startup, so the database
server must have pgvector installed.

## AI intent classifier

`app.intent_classifier` classifies questions as `poi`, `hex_score`, or
`project_info` using the multilingual
`paraphrase-multilingual-MiniLM-L12-v2` Sentence Transformers model. Create
the default classifier once during application startup, then reuse it:

```python
from app.intent_classifier import create_default_intent_classifier

classifier = await create_default_intent_classifier()
match = await classifier.classify("Що означає оцінка цього гекса?")
print(match.intent, match.similarity)
```

The model produces 384-dimensional embeddings for intent classification.
These are separate from the 1536-dimensional `ai_knowledge` table embeddings;
do not insert classifier embeddings into that table.

To try the classifier interactively from the repository root, run:

```powershell
.\.venv\Scripts\python.exe -m app.try_intent_classifier
```

For full AI answers, add a Groq API key to your local `.env` file:

```dotenv
GROQ_API_KEY=your_groq_api_key
GROQ_MODEL=openai/gpt-oss-20b
```

The configured default is `openai/gpt-oss-20b`. Set `GROQ_MODEL` to another
model ID currently available in your Groq account if desired. Groq's active
models are listed in the [Groq model catalog](https://console.groq.com/docs/models).
The tester reports Groq API errors, including rate limits, rather than
returning an incomplete answer. The API key is sent only to Groq and must not
be committed.

The first launch loads the local intent model. Enter questions to see
recognized project-related parts and parts that are ignored. For example,
`поясни оцінку цього гекса і дай рецепт сирників` separates the hex-score
question from the unrelated recipe request. For POI and hex-score questions,
the tester asks for the hex's 15-character H3 ID unless it is included in the
question. POI records for that exact H3 cell, with readable type and category
names, are sent to Groq so the model can describe them naturally. Its
prompt asks it not to invent places or add boilerplate about absent types. If
the cell has no POI records, the tester reports that directly without calling
the model. Hex-score questions fetch facts from PostgreSQL and send them to
Groq. Project-info questions use the category and POI-type catalog from
PostgreSQL. Unrelated parts are not sent to Groq.

Only the selected question and the relevant facts returned from PostgreSQL
are sent to Groq. This CLI is for local experimentation; it does not add
an API endpoint to the backend.

Parts scoring below `0.2` are reported as unrelated; pass another
`min_similarity` to `create_default_intent_classifier` to tune the threshold.
Similarity scores are not probabilities. On Windows, press Ctrl+Z and Enter
(or Ctrl+C) to exit.
## AI chat WebSocket (#60)

The chat uses Redis 6.0.9 or newer for anonymous sessions. To start a local Redis
server with Docker, run this in a separate terminal:

```sh
docker run --rm --name okil-redis -p 127.0.0.1:6379:6379 redis:7
```

Install the Python dependencies again with `python -m pip install -r requirements.txt`.
The backend defaults to `REDIS_URL=redis://127.0.0.1:6379/0`. Set `REDIS_URL` in the
backend environment to use another Redis instance. `CHAT_SESSION_TTL_SECONDS`
defaults to `1800`.

Connect to `ws://127.0.0.1:8000/ws/chat` locally or `wss://<backend-host>/ws/chat`
from an HTTPS frontend. The handshake requires an exact frontend `Origin` from
`CORS_ORIGINS`.

Once the socket opens, send `session.open` and wait for `session.ready` before
sending questions. To start a new conversation, send:

```json
{"type":"session.open"}
```

`session.ready` contains `sessionToken` and `history`. Store only the token in
the frontend's `sessionStorage`. On a new connection, send
`{"type":"session.open","sessionToken":"<stored-token>"}` to restore the chat.
Each history entry has `requestId`, `question`, and `answer`. If the backend
returns `session_expired`, remove the stored token and send `session.open`
without it. Treat the token as a secret and keep it out of URLs and logs.

To send a question, send this JSON text message:

```json
{"type":"message.send","requestId":"req_1","text":"Hello"}
```

The backend sends events for the same `requestId`:

```json
{"type":"message.accepted","requestId":"req_1"}
{"type":"answer.status","requestId":"req_1","stage":"generating"}
{"type":"answer.completed","requestId":"req_1","text":"<AI answer>"}
```

The chat adapter uses the AI classifier and `answer_intent` service from
[PR #69](https://github.com/team-overtake-project-okil/backend-okil-project/pull/69).
This branch includes the two unchanged AI modules from that PR; their Python
dependencies, intent model and `GROQ_API_KEY` must be configured. Until then the socket returns a
readable `ai_unavailable` error; it never substitutes a test reply.
See [chat AI integration](docs/chat_ai_integration.md) for setup, context,
errors, source provenance and verification. Optional `CHAT_PRELOAD_AI=true`
loads the classifier during startup so a cold load does not consume the first
reply's deadline.

It saves the completed turn
before sending `answer.completed`. One reply can run per session at a time,
including when two sockets share a token. The reply times out after 30 seconds
and is cancelled when the socket disconnects. Only completed turns appear in
history. The session stores up to 30 turns and expires after inactivity. Taking
on a new question or saving a reply refreshes the expiry; opening a socket or
restoring history alone does not.

Use a new `requestId` for each question. If the client retries the same ID and
question while that turn remains in history, the backend returns the saved
answer without `answer.status`. A different question with the same ID receives
`request_id_reused`.

Errors have `type: "error"`, a `code`, a readable `message`, and `requestId` when
a valid ID is available (`null` otherwise). Examples include `session_required`,
`session_expired`, `invalid_message`, `message_too_long`, `busy`, `reply_timeout`,
and `storage_unavailable`. The connection stays open after these errors.
Send a non-blank `requestId` of at most 64 characters and non-blank `text` of
at most 4,000 characters. JSON messages are limited to 32 KiB. To reject oversized
messages before they reach the application, run Uvicorn with
`--ws websockets-sansio --ws-max-size 32768`. In that mode Uvicorn closes the connection
with code `1009` for messages over the limit.

