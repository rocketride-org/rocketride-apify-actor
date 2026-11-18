# RocketRide: AI Pipelines & MCP Server

RocketRide is an AI Meta Harness - backed by a strong C++ engine runtime - that can help you build your agents and applications. Our goal is to help solopreneurs and pro developers out there to provide an end to end AI harness to make their ideas a reality. They do the building. We take care of everything right from auth to billing.

This Actor runs the open-source **RocketRide engine** on Apify as an always-warm **MCP server** and **HTTP API**. Your AI agent (Claude, Cursor, ChatGPT, n8n, LangChain or your own code) gets one endpoint where it can **build, validate, run and trace AI pipelines**. It can also call ready-made **document pipelines** that turn PDFs, Office files, HTML and images into clean text or **redact personal data**, with no API keys needed.

<a href="https://rocketride.org"><img src="https://raw.githubusercontent.com/rocketride-org/rocketride-server/develop/docs/public/assets/pipeline-example.png" alt="RocketRide visual pipeline builder" width="100%"></a>

## What can this RocketRide Actor do?

- 🤖 **MCP server for agents.** Point any MCP client at `/mcp` to get RocketRide's full tool set: discover 100+ nodes (LLMs, vector DBs, OCR, NER, PII, chunking, embeddings, tools), validate pipelines, run them, stream results and trace every step.
- 📄 **Document to text.** Send a PDF, DOCX, PPTX, XLSX, HTML or image and get clean text back, with OCR for embedded images.
- 🛡️ **PII redaction.** Parse, OCR and detect named entities, then get text back with names, emails, phones and addresses anonymized.
- ⚡ **Warm, not cold.** The Actor runs in Apify **Standby (Server) mode**, so the C++ engine stays up between calls with no container start per request.
- 🔑 **Bring your own models.** Add OpenAI, Anthropic, Gemini or any other provider key as encrypted input. Keys stay in your own run.
- 🧩 **Apify platform built in.** You get API access, monitoring, logs, usage limits and integrations, and results can be saved to a dataset.

## How to use RocketRide on Apify

1. Click **Try for free** (or **Start**) to open the Actor in Apify Console. You can optionally add LLM provider keys in the input.
2. Copy your **Apify API token** from [Settings → API & Integrations](https://console.apify.com/settings/integrations).
3. Call the Actor's **Standby URL**: `https://rocketride--rocketride.apify.actor`. The first request starts the server, and later requests hit the warm engine.
4. Connect your agent to `https://rocketride--rocketride.apify.actor/mcp`, or call the HTTP endpoints below.

Every request needs `Authorization: Bearer <APIFY_TOKEN>`.

### Connect an MCP client

**Claude Code**

```bash
claude mcp add --transport http rocketride https://rocketride--rocketride.apify.actor/mcp \
  --header "Authorization: Bearer $APIFY_TOKEN"
```

**Cursor, VS Code, Claude Desktop and other clients** (`mcp.json`)

```json
{
  "mcpServers": {
    "rocketride": {
      "url": "https://rocketride--rocketride.apify.actor/mcp",
      "headers": { "Authorization": "Bearer <APIFY_TOKEN>" }
    }
  }
}
```

Then ask your agent something like *"Use RocketRide to build a pipeline that extracts invoices from PDFs and summarizes them with GPT-4o, validate it, and run it on this file."* The full tool reference is at [docs.rocketride.org](https://docs.rocketride.org/connect/mcp/http/tools).

## Featured pipelines (HTTP API)

| Endpoint | What it does | Keys needed |
| --- | --- | --- |
| `POST /pipelines/doc-to-text` | Any document → clean text, with OCR for images | None |
| `POST /pipelines/redact-pii` | Document → NER → text with PII anonymized | None |
| `GET /pipelines` | Lists featured pipelines with their `.pipe` definitions | None |
| `POST /run` | Runs any webhook-source `.pipe` once | Depends on the pipeline |
| `POST /mcp` | RocketRide MCP server (Streamable HTTP) | Depends on the tools used |
| `/engine/...` | Pass-through to the engine's native HTTP API | Depends |
| `GET /health` | Engine status | None |

You can send input three ways: **raw bytes** (set `Content-Type`), **multipart/form-data** with a file field, or **JSON** with `{"text": "..."}` or `{"url": "https://..."}`. Add `?save=true` to also store the result in the run's dataset.

### Example: extract text from a PDF by URL

```bash
curl -X POST "https://rocketride--rocketride.apify.actor/pipelines/doc-to-text" \
  -H "Authorization: Bearer $APIFY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://arxiv.org/pdf/1706.03762"}'
```

### Example: redact PII from a local file

```bash
curl -X POST "https://rocketride--rocketride.apify.actor/pipelines/redact-pii?filename=contract.docx" \
  -H "Authorization: Bearer $APIFY_TOKEN" \
  -H "Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document" \
  --data-binary @contract.docx
```

### Output example

```json
{
  "pipeline": "redact-pii",
  "filename": "input.txt",
  "contentType": "text/plain",
  "bytes": 62,
  "durationMs": 840,
  "text": "Contact <PERSON> at <EMAIL> in <LOCATION>.",
  "result": { "objectsRequested": 1, "objectsCompleted": 1, "objects": { "body": { "text": ["..."] } } }
}
```

### Run your own pipeline

Design a pipeline visually in the [RocketRide VS Code extension](https://marketplace.visualstudio.com/items?itemName=RocketRide.rocketride) and give it a **webhook** source. Then send the `.pipe` JSON along with your data:

```bash
curl -X POST "https://rocketride--rocketride.apify.actor/run" \
  -H "Authorization: Bearer $APIFY_TOKEN" -H "Content-Type: application/json" \
  -d "{\"pipeline\": $(cat my-pipeline.pipe), \"text\": \"Summarize this...\"}"
```

## Input: provider keys

All input is optional. Pipelines refer to keys as `${ROCKETRIDE_...}` variables.

| Input field | Becomes | Used by |
| --- | --- | --- |
| OpenAI API key | `ROCKETRIDE_OPENAI_KEY` | `llm_openai`, embeddings, TTS |
| Anthropic API key | `ROCKETRIDE_ANTHROPIC_KEY` | `llm_anthropic` |
| Gemini API key | `ROCKETRIDE_GEMINI_KEY` | `llm_gemini` |
| Other node credentials (JSON) | any `ROCKETRIDE_*` name | vector DBs, Cohere, Exa, GitHub and more |

Secret fields are encrypted by Apify and only decrypted inside your run.

## How much does it cost to use RocketRide on Apify?

The Actor itself is **free**. You pay only for **Apify platform usage** (compute units) while your server is running, plus whatever your own LLM providers charge. Each user gets their **own isolated server** that runs in their Apify account (single-tenant).

- The server starts on the first request and stops after the idle timeout you set in **Settings → Server**.
- **4 GB of memory** is a good default. OCR and NER models need at least 2 GB.
- New Apify accounts get monthly free credits, which are enough to try the featured pipelines.

## FAQ

### Is RocketRide open source?

Yes. The engine is [MIT-licensed on GitHub](https://github.com/rocketride-org/rocketride-server). Everything this Actor runs is in that repo, and the Actor's own source is in [rocketride-org/rocketride-apify-actor](https://github.com/rocketride-org/rocketride-apify-actor).

### How is this different from RocketRide Cloud?

[RocketRide Cloud](https://rocketride.org) is our fully managed service with team features and deployments. This Actor gives you a private engine inside your Apify account, billed through Apify. It suits agents and automations that already run on Apify.

### Is my data private?

Each run is single-tenant. Your files, pipelines and keys stay inside your own Actor run. Requests reach the server only with an Apify token that has access to your account.

### Known limitations

- Upload and "dropper" links that the MCP tools generate point inside the container. To get files in from outside, use `/pipelines/*` or `/run` with a URL or bytes instead.
- `send_files` is not offered over MCP because the engine sits behind Apify's proxy. Use `send_data` or the HTTP endpoints instead.
- State lives in the run's memory, so a run restart clears running pipelines. Save results to a dataset with `?save=true`.

### Can I use this from n8n, Make or Zapier?

Yes. Call the HTTP endpoints with your Apify token. RocketRide also ships an n8n community node.

## Support

- Questions, bugs and feature requests: open an issue in the **Issues** tab or on [GitHub](https://github.com/rocketride-org/rocketride-apify-actor/issues).
- Community: [RocketRide Discord](https://discord.gg/PMXrtenMsY)
- Docs: [docs.rocketride.org](https://docs.rocketride.org/) · LLM-friendly docs: [llms.txt](https://docs.rocketride.org/llms.txt)
- Custom pipelines or enterprise deployments: [rocketride.org](https://rocketride.org)
