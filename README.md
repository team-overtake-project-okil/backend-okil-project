# backend-okil-project

After downloading and opening in code redactor

-paste command pip install -r requirements.txt in terminal 

-copy file .env.example and name it .env in that file put url from telegram instead of PUTDATABASEURL

For mobility scoring and routes, add your provider credentials to `.env`:

```dotenv
TRAVELTIME_APP_ID=your-traveltime-application-id
TRAVELTIME_API_KEY=your-traveltime-api-key
HERE_ROUTING_API_KEY=your-here-api-key
```

Get the first two values from your TravelTime API account and the HERE key from
your HERE developer project. `TRAVELTIME_TIME_CONSTANT_SECONDS` and
`TRAVELTIME_MAX_SEARCH_SECONDS` are optional tuning settings already shown in
`.env.example`. Restart the backend after editing `.env`. Do not commit or share
`.env`; it is git-ignored.

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
{"type":"answer.completed","requestId":"req_1","text":"Test reply: Hello"}
```

The backend currently uses a test reply service. It saves the completed turn
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
