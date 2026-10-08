# Local VS Code testing guide

Run from the repository root. This milestone preserves HTTP/WS contracts but does
not add authentication or persistent sessions. Bind local manual testing to loopback.
No MongoDB access or paid inference is needed for the automated suite.

## Windows setup

Open the folder in VS Code. Use **Python: Select Interpreter** and select
`.venv\Scripts\python.exe`. Existing environments can skip creation/install.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install pip==26.2.1
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
```

Direct interpreter commands avoid PowerShell activation/execution-policy issues.
`tests/conftest.py` selects mock inference and offline Hugging Face mode before
application imports. API tests replace RAG warmup/inference with fixtures; the
graph regression uses the real LangGraph loop with mock LLM and stubbed retrieval.
Tests do not read or write MongoDB. Temporary catalogue fixtures are isolated.

Run individual areas:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_product_tools.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_api_reliability.py tests/test_session_reset.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_brain_reliability.py -q
```

## Manual HTTP and WebSocket smoke checks

In a separate terminal, use process-local overrides (do not overwrite `.env`):

```powershell
$env:LLM_PROVIDER = 'mock'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:MAX_MESSAGE_CHARS = '4000'
.\.venv\Scripts\python.exe -m uvicorn main:api --host 127.0.0.1 --port 5007
```

Existing RAG tries to load local cached embeddings; without a cache it reports a
warmup failure and retrieval is skipped. This does not affect deterministic tests.
Do not run ingestion to work around it unless you intend to build local artifacts.
The health endpoint currently reports process health, not validated product readiness.

Open `http://127.0.0.1:5007/docs` for generated OpenAPI. Example PowerShell requests:

```powershell
Invoke-RestMethod http://127.0.0.1:5007/health
$body = @{message='What is the price of a mug?'; session_id='local-review'} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:5007/chat -Method Post -ContentType 'application/json' -Body $body
$reset = @{session_id='local-review'} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:5007/session/reset -Method Post -ContentType 'application/json' -Body $reset
```

Successful chat still returns `{"reply":"...","session_id":"local-review"}`.
Current JSON catalogue prices are snapshots; stock is unknown without a recent
observation. No live inventory guarantee is implied. Server-generated request IDs
appear in `X-Request-ID`. Do not paste credentials into screenshots or bug reports.

From a browser console on a local page, connect to `ws://127.0.0.1:5007/ws/local-review`,
send `{"message":"hello"}` or plain `hello`, and expect `token` then `done` events.
Send `{"message":12}` to verify an `error` event and continued connection usability.
Do not expose the socket publicly: authentication is a remaining release blocker.

## Advisory scan without changing application dependencies

```powershell
python -m venv .venv-security
.\.venv-security\Scripts\python.exe -m pip install pip-audit==2.10.1
.\.venv-security\Scripts\python.exe -m pip_audit --path .venv/Lib/site-packages --format json --output docs/dependency-audit.json
```

The scanner queries public advisory services using package names/versions. Exit 1
means vulnerabilities or another reported failure; inspect output rather than hiding
it. No `--fix` bulk upgrade is used. Re-run tests after any targeted remediation.

## Ubuntu local checks

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pip==26.2.1
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest tests -q -p no:cacheprovider
LLM_PROVIDER=mock HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python -m uvicorn main:api --host 127.0.0.1 --port 5007
```

This is a local testing command, not a production deployment recommendation.
The existing two-worker deployment example is blocked until shared session state
and ownership enforcement exist. See `GREENY_DEPLOYMENT_CHECKLIST.md`.

## Known test limits

No live models, embedding quality, auth, persistent restart, consent submission,
duplicate-lead protection or engagement cooldown behavior are certified by these
tests. Hindi/Hinglish coverage here checks Unicode input preservation, not language
understanding. Broader sales scenarios remain planned. A community-integration
deprecation warning is expected and tracked separately from test failures.
