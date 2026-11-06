# Connector SDK — add a provider in under an hour

Every connector lives in `connexa/connectors/`, is registered in
`connectors/registry.py`, and instantly appears in the catalog, agent tool
packs, tool search, the security gateway and credit metering. You write the
provider-specific part; the platform supplies the rest:

| You get for free | Where |
|---|---|
| demo lane when no credentials exist | `Connector` + `credential_token` |
| `reauth_required` machine-actionable errors | `validate_credential` meta-tool |
| OAuth social-login (authorize URL, signed state, code exchange, refresh) | `configure_oauth` / `exchange_code` |
| paste-token lane + platform env-token lane | `credential_token` priority |
| metering, audit, security-gateway input/output scanning | agent plane |

## Recipe 1 — plain REST provider (declarative)

For providers that are just REST + a bearer token, use `sdk.py`. Create
`connexa/connectors/real_todo.py`:

```python
from pydantic import BaseModel, Field
from .sdk import RestCall, SimpleRestConnector


class CreateTaskInput(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    due: str = ""                     # ISO date, optional


class TodoCloudConnector(SimpleRestConnector):
    slug = "todocloud"
    name = "TodoCloud"
    description = "List and create tasks in TodoCloud."

    def __init__(self, env_token: str = "", oauth_config: dict | None = None) -> None:
        super().__init__(
            api_base="https://api.todocloud.example",
            env_token=env_token,
            oauth_config=oauth_config,
            scopes="tasks.read tasks.write",                     # provider-specific
            calls=[
                RestCall(
                    name="list_tasks", description="List the user's tasks.",
                    path="/v1/tasks", method="GET",
                    demo_results=[{"id": "demo-1", "title": "Demo task"}],
                ),
                RestCall(
                    name="create_task", description="Create a task.",
                    path="/v1/tasks", method="POST", read_only=False,
                    input_model=CreateTaskInput, credit_type="medium",
                    demo_results=[{"created": True}],
                ),
            ],
        )
```

Register it:

```python
# connectors/registry.py
from .real_todo import TodoCloudConnector
...
TodoCloudConnector(settings.todo_token, settings.todo_oauth),
```

Add the env vars to `config.py` / `.env.example`, restart, done: agents can
call `todocloud__list_tasks` and `todocloud__create_task`, and
`POST /api/v1/registered-users/{ru}/credentials/` accepts pasted tokens
immediately.

## Recipe 2 — provider with quirks (full class)

When requests are non-trivial (MIME building, multi-step calls, unusual OAuth),
subclass `Connector` and write tools by hand. `real_gmail.py` (MIME + Google
refresh semantics) and `real_slack.py` (auto-join before send) are the
reference implementations — copy whichever is closest.

The three override points you will actually use:

- `exchange_code(code, redirect_uri)` — providers with non-standard token
  endpoints (Notion uses HTTP Basic + JSON; Google/Slack/GitHub use the base
  implementation)
- `describe_token(token)` — probe the provider for a human account label
  (email, workspace name, login) shown in validation results
- `maybe_refresh(creds)` — only needed for non-standard refresh semantics;
  Google/GitHub-style refresh is already built in

## Checklist (both recipes)

1. `connectors/real_<slug>.py` — the connector
2. `config.py` — `_<slug>_token` + `_<slug>_oauth` env reads
3. `connectors/registry.py` — instantiate with `settings.*`
4. `.env.example` (+ your local `.env`) — the new vars
5. If the provider has OAuth: add `http://localhost:8000/oauth/<slug>/callback`
   as a redirect URI in the provider's app settings
6. Restart and smoke: `/llms.txt` lists it, `POST /api/v1/agent/signup/`
   returns a pack containing `authenticate_<slug>` + `<slug>__validate_credential`,
   and calling a tool without credentials returns the demo lane (or
   `reauth_required` on validate)
