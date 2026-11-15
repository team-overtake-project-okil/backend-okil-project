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


## Hex debugger

Set DEBUG_ENABLED=true, DEBUG_USER and DEBUG_PASSWORD in the backend environment and restart the API. Use a team credential over HTTPS on shared deployments. The frontend route is /admin/hex-debug and needs the usual API URL and Mapbox token. Configure the frontend host to serve index.html at this path. The debugger reads the current point, stop, route and hexagon data. Its POST requests calculate temporary scores.


Debug authentication uses `POST /debug/session` with HTTP Basic credentials to issue a 15-minute bearer token. All other `/debug` endpoints require that token. Five failed logins from one client IP within five minutes block logins from that IP for fifteen minutes. Changing usernames does not bypass the limit.

Keep credentials in backend hosting secrets, outside Git and frontend environment variables. HTTPS is required by default. For local development only, `DEBUG_ALLOW_LOCAL_HTTP=true` permits HTTP from loopback clients to localhost. Leave it false on shared deployments, terminate TLS at the hosting proxy, and trust forwarded headers only from that proxy.

Sessions and login limits currently live in one backend process and reset when it restarts. Run one worker; multiple workers or instances require a shared session and rate-limit store first.
