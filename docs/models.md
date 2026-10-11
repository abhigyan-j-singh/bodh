# Models and providers

bodh talks to a model in four roles, each named in `config.yaml`:

| Role | Key | What it does | Default |
| --- | --- | --- | --- |
| judge | `models.judge` | reads every article once: the judgment, the score, the band tag | `claude-sonnet-5-5` |
| reviewer | `models.reviewer` | re-reads a close call with the full text; can only save an article from a cut | `claude-opus-5-5` |
| profiler | `models.profiler` | rewrites `profile/learned.md` on Sundays | `claude-opus-5-5` |
| quick judge | `catch_up.model` | what `bodh catch-up` uses to get through a backlog cheaply | `claude-haiku-5-5` |

A model is written `provider/model-id`. A bare id means Anthropic, so a `config.yaml` from
before October 2026 still reads the same. The providers:

| Provider | Models written as | Key in `.env` | Batch API | Structured output | Prompt cache |
| --- | --- | --- | --- | --- | --- |
| `anthropic` | `claude-sonnet-5-5` or `anthropic/claude-sonnet-5-5` | `ANTHROPIC_API_KEY` | yes (half price) | yes | yes, 5 minutes or an hour |
| `openai` | `openai/gpt-5-mini` | `OPENAI_API_KEY` | yes (half price) | yes | automatic |
| `gemini` | `gemini/gemini-2.5-pro` | `GEMINI_API_KEY` | yes (half price) | yes | automatic |
| `openrouter` | `openrouter/qwen/qwen3-235b` (the model's own slashes stay) | `OPENROUTER_API_KEY` | no | where the model allows; plain JSON otherwise | no |
| `openai-compatible` | `openai-compatible/llama3.3` | `OPENAI_COMPATIBLE_BASE_URL`, key optional | no | where the service allows; plain JSON otherwise | no |

The database stores a judgment under the model's name as written above (`claude-sonnet-5-5`
for Anthropic, `openai/gpt-5-mini` for the rest), and `models.prices_per_mtok` is keyed the
same way. Without a price a model is charged as the dearest one listed, with a warning, so an
unlisted model never slips past the daily budget at $0.

## Keys

Only the providers the four roles use need a key; `bodh doctor` names the missing ones and
which role needs each. `bodh keys` lists what is set (never a value, never a prefix);
`bodh keys set openai` asks for the key with hidden input and writes `.env` with owner-only
permissions, replacing the line if there is one. A key may also sit in the environment, or in
the system keychain (the macOS Keychain, the Linux secret service, the Windows Credential
Manager) with `pip install bodh[keychain]` and `bodh keys set openai --keychain`. The lookup
order is `.env`, then the environment, then the keychain, under the service `bodh` and the
variable's name. Logs and error messages replace every secret value with `[secret]`.

## A local model

`openai-compatible` is any service that speaks the OpenAI Chat Completions API: Ollama,
llama.cpp's server, vLLM, LM Studio. The base URL selects it:

```
OPENAI_COMPATIBLE_BASE_URL=http://localhost:11434/v1     # Ollama
```

and `models.judge: openai-compatible/llama3.3` (whatever `ollama list` shows). No key is needed
for Ollama; a self-hosted vLLM behind a token takes `OPENAI_COMPATIBLE_API_KEY`. With every
role on a local model nothing leaves the machine but the BazQux calls. Two things to know: a
service that reports no token usage gets its spend estimated at four characters a token, and
`bodh progress` says so; and a small model may not follow the judgment schema, in which case
bodh falls back to plain JSON after the first rejection and drops any judgment that does not
parse (`bodh progress` counts them as "given up" after repeated failures).

## Trying a model before switching

```
bodh models                                   # the roles, the keys present, the prices, the capabilities
bodh models compare --with openai/gpt-5-mini  # judge 50 already-judged unread articles with both
```

A comparison re-judges a sample with the configured judge and with the second model and
prints how often the two land in the same band, how often within one band, the mean score
difference, the ten largest disagreements with both lessons, the cost per 1,000 articles and
the seconds per article. Nothing stored changes but the costs (purpose `compare`). The switch
is `models.judge` in `config.yaml`. After a switch, what the previous judge rated
`catch_up.rejudge_at` or more is re-judged on the next runs; `bodh run --rejudge` (or
`bodh catch-up --rejudge` for a backlog) re-judges everything the previous judge rated,
whatever the score. When two models have each judged twenty or more articles in the last
thirty days the dashboard's calibration section adds a table per model: how many it judged, the mean score, how often its
articles were read or starred, the student's grades for and against.

## What was tested, and how

| Provider | Checked against |
| --- | --- |
| Anthropic | daily use since 2025 with Sonnet and Opus; the contract suite over a fake of the SDK |
| OpenAI, Gemini, OpenRouter, OpenAI-compatible | the contract suite (`tests/contracts/test_provider.py`), twelve behaviours each, over fakes of their HTTP APIs written from the published request and response shapes; not yet exercised against the live services from this checkout |

The contract suite is what a provider must pass: a judgment call, a completion, structured
output and the plain-JSON fallback, a batch submitted, polled and collected, a result that
errored or expired, a 429 and a 5xx retried with backoff, a connection error, a rejected key
and an unknown model id stopping the run with the name and never the key, and usage recorded
as the service reported it (or estimated when it reports nothing). A live check of a new
provider is one line in `docs/live-checks.md`: `bodh models compare --with <model> --sample 5`.

## Failure behaviour

A rejected key, a forbidden request or a model id the provider does not know stops the run
with exit code 4 and the message names the model (never the key). A 429 or 5xx is retried
with backoff and jitter, three attempts in a run and nine in a catch-up; a call that still
fails leaves its articles pending for the next run. Five failed calls in a row stop the
judging for that run (the provider is down, or the prompt is broken for that model): the
rest stays pending, the run is still marked ok and its summary says why; a catch-up stops
after that round instead of spinning. The daily budget (`models.daily_budget_usd`) is one
figure across every provider.

## Adding a provider

One file in `src/bodh/providers/`, a subclass of `Provider` (or of `HttpProvider`, which
brings the HTTP client, the retry and the error translation), with `capabilities`, `judge`,
`complete`, `request`, and the three batch methods (`submit_batch` may raise `ModelError` when
there is no batch API; `batch_status` and `collect_batch` then raise `BatchNotFound`). Register
it in `providers/__init__.py` (`make_provider`) and `providers/base.py` (`PROVIDERS` and
`REQUIRED_SECRET`), add the secret to `config.Secrets` and `SECRET_FIELDS`, write a fake of
the service in `tests/fakes.py`, and add it to the contract suite's list. The suite passing is
the whole acceptance test.
