# RocketRide - Meta harness

RocketRide is an AI Meta Harness - backed by a strong C++ engine runtime - that can help you build your agents and applications. Our goal is to help solopreneurs and pro developers out there to provide an end to end AI harness to make their ideas a reality. They do the building. We take care of everything right from auth to billing.

<a href="https://rocketride.org"><img src="https://raw.githubusercontent.com/rocketride-org/rocketride-apify-actor/main/assets/rocketride-ai-meta-harness.jpg" alt="RocketRide, the open source AI meta-harness" width="100%"></a>

This Actor puts the open-source **RocketRide meta-harness** on Apify as an always-warm **MCP server** and **HTTP API**. One endpoint gives your AI agent (Claude, Cursor, ChatGPT, n8n, LangChain or your own code) two things:

- **A harness to build with.** 33 MCP tools to discover about 150 nodes, then compose, validate, run and trace **AI pipelines and agents** on RocketRide's C++ engine.
- **Pipelines that already work.** Six ready-made endpoints for documents and agents: **extract text**, **redact PII**, **summarize**, **extract structured data**, **ask a document**, and a **research agent**.

## What can RocketRide - Meta harness do?

| Ready-made pipeline | What you get | LLM key needed |
| --- | --- | --- |
| 📄 `doc-to-text` | Clean text from PDF, DOCX, PPTX, XLSX, HTML and images, with OCR | No |
| 🛡️ `redact-pii` | The same text with detected names, organizations, places and other personal details masked | No |
| 📝 `summarize` | Summary, key points and named entities | Yes |
| 🧾 `extract-data` | The fields you name, returned as JSON rows | Yes |
| ❓ `ask` | An answer to your question, grounded in the document | Yes |
| 🤖 `agent` | A RocketRide agent that plans, fetches web pages and APIs, and answers | Yes |

And through the **MCP server**, your agent can build anything else:

- 🧩 **About 150 nodes.** LLMs (OpenAI, Anthropic, Gemini, Mistral, Bedrock, Ollama and more), agents (RocketRide, LangChain, CrewAI, LlamaIndex), vector, graph and SQL stores, OCR, NER, chunking, embeddings, and tools such as HTTP, Python, GitHub, Slack, Notion and Apify.
- ✅ **Validated before it runs.** The engine checks every pipeline against its own rules, so agents get precise errors instead of silent failures.
- 🔍 **Traceable.** Watch running pipelines and read back what each step did.
- ⚡ **Warm, not cold.** The Actor runs in Apify **Standby (Server) mode**. The engine and your pipelines stay loaded between requests.
- 🔑 **Bring your own model.** OpenAI, Anthropic, Gemini or any OpenAI-compatible endpoint (OpenRouter, Groq, Together, Ollama). Keys are encrypted and stay inside your own run.

<a href="https://docs.rocketride.org/"><img src="https://raw.githubusercontent.com/rocketride-org/rocketride-apify-actor/main/assets/rocketride-visual-pipeline-builder.jpg" alt="A RocketRide AI pipeline in the visual builder" width="100%"></a>

## How to use RocketRide on Apify

1. Open the Actor in Apify Console. To use the LLM pipelines, add one provider key in the **Input** tab.
2. Copy your **Apify API token** from [Settings → API & Integrations](https://console.apify.com/settings/integrations).
3. Send requests to the server URL: `https://rocketrideai--rocketride.apify.actor`. The first request starts your server; later requests hit the warm engine.
4. Point your agent at `/mcp`, or call the pipeline endpoints below.

Every request needs the header `Authorization: Bearer <APIFY_TOKEN>`.

## Connect your agent over MCP

**Claude Code**

```bash
claude mcp add --transport http rocketride https://rocketrideai--rocketride.apify.actor/mcp \
  --header "Authorization: Bearer $APIFY_TOKEN"
```

**Cursor, VS Code, Claude Desktop and other clients** (`mcp.json`)

```json
{
  "mcpServers": {
    "rocketride": {
      "url": "https://rocketrideai--rocketride.apify.actor/mcp",
      "headers": { "Authorization": "Bearer <APIFY_TOKEN>" }
    }
  }
}
```

Then ask your agent something like:

> Use RocketRide to build a pipeline that takes a PDF invoice, extracts the vendor, total and due date with GPT-4o mini, and returns JSON. Validate it, then run it on this file.

The MCP tools cover the whole loop: `list_components`, `describe_component`, `validate_pipeline`, `run_pipeline`, `send_data`, `monitor`, `terminate` and more. See the [tool reference](https://docs.rocketride.org/connect/mcp/http/tools).

## Pipeline API examples

Send a document as **JSON** (`{"text": "..."}` or `{"url": "https://..."}`), as **raw bytes** with a `Content-Type`, or as **multipart/form-data**. With raw or multipart uploads, pass options in the query string. Add `?save=true` to also store the result in the run's dataset.

### Summarize a PDF

```bash
curl -X POST "https://rocketrideai--rocketride.apify.actor/pipelines/summarize" \
  -H "Authorization: Bearer $APIFY_TOKEN" -H "Content-Type: application/json" \
  -d '{"url": "https://arxiv.org/pdf/1706.03762", "summaryWords": 150}'
```

The `text` field comes back with a summary paragraph, a `Key Points:` list and an `Entities:` list.

### Extract structured data from an invoice

```bash
curl -X POST "https://rocketrideai--rocketride.apify.actor/pipelines/extract-data?fields=invoice_number,total,due_date" \
  -H "Authorization: Bearer $APIFY_TOKEN" -H "Content-Type: application/pdf" \
  --data-binary @invoice.pdf
```

Example output:

```json
{
  "pipeline": "extract-data",
  "filename": "invoice.pdf",
  "durationMs": 2140,
  "data": [{ "invoice_number": "INV-2026-0042", "total": "960.00", "due_date": "2026-12-01" }]
}
```

For typed columns, send JSON instead: `"fields": [{"name": "total", "type": "decimal"}, {"name": "due_date", "type": "date"}]`.

### Ask a question about a document

```bash
curl -X POST "https://rocketrideai--rocketride.apify.actor/pipelines/ask" \
  -H "Authorization: Bearer $APIFY_TOKEN" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/contract.pdf", "question": "What is the notice period?"}'
```

The answer is in the `answer` field.

### Run the research agent

```bash
curl -X POST "https://rocketrideai--rocketride.apify.actor/pipelines/agent" \
  -H "Authorization: Bearer $APIFY_TOKEN" -H "Content-Type: application/json" \
  -d '{"question": "What changed in the latest Python release? Check python.org and cite the page."}'
```

### Redact personal data (no key needed)

```bash
curl -X POST "https://rocketrideai--rocketride.apify.actor/pipelines/redact-pii" \
  -H "Authorization: Bearer $APIFY_TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "Contact John Smith at john.smith@example.com in Seattle."}'
```

```json
{ "pipeline": "redact-pii", "text": "Contact ██████████ at ██████████@███████████ in ███████." }
```

### All endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /pipelines/{name}` | Run a ready-made pipeline |
| `GET /pipelines` | List pipelines, their options and your configured LLM providers |
| `GET /pipelines/{name}` | Get the `.pipe` definition a request would run, to reuse or edit |
| `POST /run` | Run any webhook-source `.pipe` once: `{"pipeline": {...}, "text": "..."}` |
| `POST /mcp` | RocketRide MCP server (Streamable HTTP) |
| `/engine/...` | Pass-through to the engine's native HTTP API |
| `GET /health` | Engine status |

### Pipeline options

| Option | Pipelines | Meaning |
| --- | --- | --- |
| `provider` | LLM pipelines | `openai`, `anthropic`, `gemini` or `openai-compatible`. Default: the first one you configured |
| `model` | LLM pipelines | Model id at that provider, e.g. `gpt-4o-mini`. Default: the node's default model |
| `ocr` | summarize, extract-data, ask | `true` to OCR scanned pages and images. Default `false` |
| `question` | ask, agent | Required |
| `fields` | extract-data | Required. Column names, or objects with `name`, `type`, `default` |
| `summaryWords`, `keyPoints`, `entities`, `chunks` | summarize | Length and sections of the summary |
| `instructions`, `web`, `maxSteps` | agent | System prompt, HTTP tool on or off, max plan/act rounds |

## Input: bring your own model

All input is optional. The document pipelines and the MCP tools for building and validating pipelines work with no keys at all.

| Input field | Pipelines see it as |
| --- | --- |
| OpenAI API key | `${ROCKETRIDE_OPENAI_KEY}` |
| Anthropic API key | `${ROCKETRIDE_ANTHROPIC_KEY}` |
| Google Gemini API key | `${ROCKETRIDE_GEMINI_KEY}` |
| OpenAI-compatible endpoint URL, model and key | `${ROCKETRIDE_LLM_BASE_URL}`, `${ROCKETRIDE_LLM_MODEL}`, `${ROCKETRIDE_LLM_KEY}` |
| Default model | Used when a request names no `model` |
| Other node credentials (JSON) | Any `ROCKETRIDE_*` name, for vector stores, search, GitHub and more |

Secret fields are encrypted by Apify and decrypted only inside your run. Pipelines refer to keys by variable name, so a key never appears in a pipeline definition.

## How much does it cost to use RocketRide on Apify?

The Actor is **free**. You pay for **Apify platform usage** while your server runs, plus whatever your own LLM provider charges.

- Each user gets a **private server** in their own Apify account (single-tenant).
- The server starts on the first request and stops after the idle timeout you set in **Settings → Server**.
- **4 GB of memory** is the recommended setting. The OCR and entity models need at least 2 GB.
- Apify's free plan credits are enough to try every pipeline.

## Performance

- **Warm requests are fast.** Once a pipeline is running, a short text document takes a fraction of a second, plus LLM time. OCR and entity detection take longer.
- **A new pipeline takes a few seconds to start.** LLM pipelines are built from your options. The three most recent stay running, so repeating a request with the same options skips the start.
- **The first start on a new machine is the slow one.** Apify has to download the image (the engine plus OCR and entity models) before the server can boot. That can take a few minutes; after that the server boots in seconds.

## FAQ

### What is a meta-harness?

An agent harness runs one agent. A meta-harness runs the whole stack around it: models, tools, memory, data stores, document processing and other agent frameworks, wired together as a pipeline you can validate, observe and deploy. RocketRide runs those pipelines on a multithreaded C++ engine.

### Is RocketRide open source?

Yes. The engine is [MIT-licensed on GitHub](https://github.com/rocketride-org/rocketride-server). This Actor's source, including every ready-made pipeline as a `.pipe` file, is in [rocketride-org/rocketride-apify-actor](https://github.com/rocketride-org/rocketride-apify-actor).

### Can I edit the ready-made pipelines?

Yes. `GET /pipelines/{name}` returns the pipeline definition. Open it in the [RocketRide VS Code extension](https://marketplace.visualstudio.com/items?itemName=RocketRide.rocketride) to edit it on a visual canvas, then run your version through `/run` or the MCP `run_pipeline` tool.

### How is this different from RocketRide Cloud?

[RocketRide Cloud](https://rocketride.org) is the managed service with team features and deployments. This Actor gives you a private engine inside your Apify account, billed through Apify. It suits agents and automations that already live on Apify.

### Is my data private?

Each server is single-tenant. Your files, pipelines and keys stay inside your own Actor run, and only requests carrying a token for your Apify account reach it.

### Known limitations

- The server runs on CPU. Nodes that need a GPU will be slow or will not work here.
- PII detection is model-based. It reliably masked names, organizations and places in our tests, but it can miss items such as the domain of an email address. Review the output before relying on it for compliance.
- The MCP `send_files` tool and upload links read from the Actor's container, not your machine. To send your own files, use `send_data`, a pipeline endpoint, or `/run` with a URL.
- Running pipelines live in the server's memory, so a server restart clears them. Save results with `?save=true`.
- Nodes outside the ready-made pipelines install their dependencies the first time they are used, which makes that first run slower.

### Can I call this from n8n, Make or Zapier?

Yes. Use an HTTP request step with your Apify token. RocketRide also ships an n8n community node.

## Support

- Bugs and feature requests: the **Issues** tab here, or [GitHub issues](https://github.com/rocketride-org/rocketride-apify-actor/issues)
- Community: [RocketRide Discord](https://discord.gg/PMXrtenMsY)
- Docs: [docs.rocketride.org](https://docs.rocketride.org/) and [llms.txt](https://docs.rocketride.org/llms.txt) for agents
- Custom pipelines and enterprise deployments: [rocketride.org](https://rocketride.org)
