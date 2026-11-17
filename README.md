# backend-okil-project

After downloading and opening in code redactor

-paste command pip install -r requirements.txt in terminal 

-copy file .env.example and name it .env in that file put url from telegram instead of PUTDATABASEURL

## Backend data jobs

From the repository root, preview the automatic job order with
`python -m app.jobs --dry-run`, then run it with `python -m app.jobs`.
The run requires a prepared PostgreSQL database, the point-type catalog,
`SERPAPI_KEYS`, a GTFS archive and both shelter source workbooks.
See [backend job setup and scope](docs/backend_jobs.md) for prerequisites,
custom input paths, excluded manual imports and failure behavior.

For mobility scoring and routes, add your provider credentials to `.env`:

For local development without TravelTime/HERE keys, set
`EXTERNAL_TRANSPORT_ENABLED=false` in `.env` and restart the backend. Hexagon
endpoints then skip both providers, exclude mobility from the overall rating
and mark that category disabled, and return an empty `routes` list. Other
categories continue to use database scores. The default is `true`; restore it
after adding the credentials. This setting does not disable the AI provider.

```dotenv
TRAVELTIME_APP_ID=your-traveltime-application-id
TRAVELTIME_API_KEY=your-traveltime-api-key
HERE_ROUTING_API_KEY=your-here-api-key
```

Get the first two values from your TravelTime API account and the HERE key from
your HERE developer project, with both HERE Routing and Public Transit API v8
enabled for the key. HERE car details use Routing API; bus details use Public
Transit API routes, and no road-bus route is substituted when transit returns no
route. If HERE has no bus route for the requested trip, no bus route is included
in the `routes` array. TravelTime full-score thresholds default to 30 minutes
for bus and 20 minutes for car; `TRAVELTIME_TIME_CONSTANT_SECONDS` and
`TRAVELTIME_MAX_SEARCH_SECONDS` are additional tuning settings shown in
`.env.example`. Restart the backend after editing `.env`. Do not commit or
share `.env`; it is git-ignored.

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

[Окіл: база знань про механіку](docs/ai_context.md) contains standalone Ukrainian
retrieval sections with stable IDs, sample questions, factual answers and source
metadata. Export them to UTF-8 JSONL without calling a model or writing to the DB:

```powershell
python -m app.export_ai_knowledge --output okil_knowledge.jsonl
```

See [ingestion instructions](docs/ai_knowledge_ingestion.md) for the record format,
embedding dimensions and the remaining retrieval integration.

The `ai_knowledge` table stores text fragments and their 384-dimensional
embeddings in PostgreSQL with pgvector. It is intended for searchable reference
content about the project. Its `category` column is an optional topic label; it
does not classify user questions. `metadata` is JSONB for optional source and
future fields. Embeddings stored in this table must have 384 dimensions, generated
with `paraphrase-multilingual-MiniLM-L12-v2` for both documents and search queries.

For existing databases, verify that the embedding column is `vector(384)`
through a write-enabled database connection before importing vectors.
`create_all` does not alter an existing column. Regenerate incompatible existing
vectors from their source texts rather than truncating or padding them.

Import Markdown directly with local MiniLM embeddings using the configured
`DB_URL` from `.env`:

```powershell
python -m app.import_ai_knowledge --validate-only
python -m app.import_ai_knowledge --apply
```

Without `--apply`, the importer only creates embeddings and does not connect to
the DB. Repeated imports update existing document/chunk IDs without duplicates;
unrelated records and removed topics are not deleted automatically.

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
The `ai_knowledge` table uses the same 384-dimensional model for document and
search-query embeddings. Intent centroids describe request topics, not knowledge
fragments; generate a separate embedding for each document fragment.

`INTENT_DESCRIPTIONS` contains short, positive scenarios for each intent. At
initialization each scenario is encoded separately, normalized, and averaged into
one intent vector so later scenarios are not lost by encoding a long description
as a single input. Existing cosine scoring and the `0.2` threshold are preserved.
See [intent boundaries and evaluation cases](docs/intent_classification.md).

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

### Clearing a conversation (#83)

After `session.ready`, a client can clear its current session on the same socket:

```json
{"type":"session.clear","requestId":"clear_1","sessionToken":"<current-token>"}
```

Both a valid `requestId` and a valid token matching the socket's opened session
are required. The reply is
`{"type":"session.cleared","requestId":"clear_1","deleted":true}`.
`deleted:false` also confirms success: the key had already expired or was
deleted by another connection. Malformed requests, a token different from the
opened session and storage failures use the existing `error` envelope with the
clear request's `requestId`. A request without a valid ID has `requestId:null`.

Only the hashed Redis key for this token is deleted, atomically, including its
history and pending reply claim. The database is never flushed. The socket's
own reply task is cancelled before acknowledgement. Other sockets cannot save
an answer into the deleted key: their next operation returns `session_expired`.
No immediate notification is broadcast to idle sockets. Concurrent Redis updates
use WATCH and SET XX and cannot resurrect a deleted session.

On acknowledgement the frontend removes its messages and old session token,
then sends `session.open` without a token to start a new conversation. A storage
error leaves the socket's binding intact so clearing can be retried. Clients must
reconcile with the backend after a disconnect where the acknowledgement was lost.

### Connecting and sending messages

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
The AI modules originate from that PR; their Python
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


## Hex debugger

Set DEBUG_ENABLED=true, DEBUG_USER and DEBUG_PASSWORD in the backend environment and restart the API. Use a team credential over HTTPS on shared deployments. The frontend route is /admin/hex-debug and needs the usual API URL and Mapbox token. Configure the frontend host to serve index.html at this path. The debugger reads the current point, stop, route and hexagon data. Its POST requests calculate temporary scores.


Debug authentication uses `POST /debug/session` with HTTP Basic credentials to issue a 15-minute bearer token. All other `/debug` endpoints require that token. Five failed logins from one client IP within five minutes block logins from that IP for fifteen minutes. Changing usernames does not bypass the limit.

Keep credentials in backend hosting secrets, outside Git and frontend environment variables. HTTPS is required by default. For local development only, `DEBUG_ALLOW_LOCAL_HTTP=true` permits HTTP from loopback clients to localhost. Leave it false on shared deployments, terminate TLS at the hosting proxy, and trust forwarded headers only from that proxy.

Sessions and login limits currently live in one backend process and reset when it restarts. Run one worker; multiple workers or instances require a shared session and rate-limit store first.


Debugger mobility uses the same TravelTime service as `/hexagons`, including its default
bus mode and Lviv centre anchor. Configure `TRAVELTIME_API_KEY` and `TRAVELTIME_APP_ID`
on the backend. `/debug/ratings`, `/debug/hexagons/{id}` and `/debug/recalculate` accept
`transportation`, repeated `anchor_lat`, `anchor_lon` and `weight` query parameters,
matching the main API. Stored, fresh and temporary weighted results all use the current
TravelTime mobility score; legacy database mobility and GTFS connectivity are not used
for scoring. Stops remain inspectable map data but do not contribute to mobility.
TravelTime configuration or request failures return explicit 503/502 errors. Ratings
skip TravelTime when mobility is disabled; detail/recalculation still fetch it because
they include baseline scores with all default categories enabled. No scores are saved.
