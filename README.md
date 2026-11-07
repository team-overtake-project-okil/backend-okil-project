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

The first launch loads the local model. Enter questions to see recognized
project-related parts and parts that do not match an intent. For example,
`поясни оцінку цього гекса і дай рецепт сирників` should separate the hex-score
question from the unrelated recipe request. The parser splits at sentence
punctuation and common task connectors; it is a lightweight first version, not
a general-purpose language parser.

Parts scoring below `0.2` are reported as unrelated; pass another
`min_similarity` to `create_default_intent_classifier` to tune the threshold.
Similarity scores are not probabilities. On Windows, press Ctrl+Z and Enter
(or Ctrl+C) to exit.
