# Workflow architecture

The application owns two SQLite databases in its selected data directory. `jobs.sqlite` indexes run IDs and records unexpected worker failures. `checkpoints.sqlite` belongs to LangGraph's `SqliteSaver` and holds the complete research state. UUIDs isolate graph threads; an application-wide worker limit and per-thread locks prevent overlapping mutations.

`plan` requests structured neutral headings. `gather` fetches up to three initial URLs and extracts bounded factual candidates. `verify` rejects quotes absent from their claimed source. If fewer than two distinct final source URLs support the findings, a conditional edge returns to `gather` once, fetching remaining selected URLs and retrying failed fetches. Evidence extraction may run twice; planning runs once per uninterrupted node completion. The graph stops when its budget is exhausted.

`review` calls `interrupt()` before modifying state. The server validates a decision before scheduling `Command(resume=...)`, so malformed reviews do not consume the pause. Approval preserves edited headings and leads to a deterministic draft using verified claims; rejection ends without drafting. The restart path resumes checkpointed pending execution, while a human review pause remains paused. A crash before the first graph checkpoint cannot recover an unrecorded input and is reported as a failed run.

The browser reads snapshots and polls while execution is active. It receives quotations and source metadata, not entire downloaded documents. All dynamic strings are inserted as text, including downloaded titles and model outputs. Markdown is downloaded as a file and shown as plain text, rather than rendered as untrusted HTML.

Source requests use validated DNS answers directly for socket connections while preserving TLS SNI and hostname verification. Every redirect is separately validated. No ambient proxy, browser cookie, user credential, or internal URL is passed to the collector. Model calls use a fixed loopback Ollama endpoint with environment proxies and cloud tracing disabled.

This is a local development application. Saved checkpoints contain research inputs and source text; use a suitable directory for your own data. In-flight DNS resolution uses the operating system resolver, whose latency is not controlled by the per-connection socket deadline. Prompt instructions and structured output reduce injection risk, but provenance checks cannot verify whether a paraphrase faithfully expresses its quote.
