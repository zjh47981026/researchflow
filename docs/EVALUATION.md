# Evaluation

ResearchFlow evaluates workflow guarantees separately from model quality. The automated suite uses controlled sources and providers to exercise the real LangGraph graph, checkpoint store, and review interrupt without a model download or internet access.

Run the checks from the project directory:

```sh
python -m unittest discover -s tests -v
python evaluate.py
```

The evaluation prints each result and exits with a nonzero status if any control fails. It tests durable review checkpoints, approval and rejection, outline edits, bounded collection attempts, refusal of fabricated supporting quotations, and input validation.

These results do not establish research accuracy. Quote matching checks whether a quotation occurs in a retrieved source; it cannot establish that the source is truthful, that the claim accurately interprets the quotation, or that the collected sources cover the question. A successful brief therefore still requires a person to review its claims and source quality.

The offline demo uses bundled examples and is explicitly labeled in the interface. Live mode uses the configured local Ollama model and supplied public source URLs. Model output can vary, and a workflow can correctly stop when it cannot obtain enough validated evidence.

## Verified local run (2026-10-03)

The full suite passed 48 tests. The deterministic evaluator passed 8/8 controls. A live local `qwen3:4b` run fetched the two official documentation Markdown pages, accepted seven matched quotations across two sources, rejected one nonmatching quotation, paused for review, and completed after approval. See [the smoke report](live-smoke.json). This is one integration check, not a measurement of model accuracy.

The first live check correctly stopped when the LangChain HTML page exceeded the one-megabyte download limit. The starter live example now uses the official Markdown endpoints, which fit within the collector's budget.
