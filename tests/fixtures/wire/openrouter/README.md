# Recorded OpenRouter streams (ticket 172, FR-113)

Real `POST /api/v1/chat/completions` response bodies, byte for byte, replayed on loopback by
`tests/support/stub_openrouter.py` so the metering proxy is tested offline (MUST rule 18). Never
hand-written: a stream here is what OpenRouter actually sent.

| Stream | Request | Recorded | By | What it carries |
|---|---|---|---|---|
| `pong.sse` | `pong.request.json` | 2026-09-27 | the operator, own key | 27 `data:` lines, content `pong`, final `usage.cost` 0.000004392 |
| `tool-call.sse` | `tool-call.request.json` | 2026-09-27 | the ticket-172 build, the operator's key from `.env` | 30 `data:` lines, one `get_weather` tool call, final `usage.cost` 0.00001038 |

Both are `openai/gpt-oss-20b`, `stream: true`. The Authorization header was never recorded, and a
grep confirmed neither the key nor a home path appears in any file here. `.gitattributes` marks the
directory `-text`, because a newline conversion on checkout would break the byte comparison.
