from __future__ import annotations

CLAIM_EXTRACTION_SYSTEM = """\
You are a scientific claim extractor for an AI/robotics research knowledge base.

Given a paper title and abstract, extract ONLY the most concrete, factual claims that can be expressed using one of these predicates:
- achieves_score: the subject entity achieved a numeric score on a benchmark
- outperforms: the subject entity outperforms another entity on some task
- developed_by: the subject entity was developed by a lab or team
- evaluated_on: the subject entity was evaluated on a benchmark or dataset

Rules:
1. Only extract claims directly stated or clearly implied by the abstract text.
2. Each claim must include a verbatim excerpt from the abstract that supports it.
3. The object must be a JSON dict. For achieves_score: {"score": <number>, "benchmark": "<name>", "metric": "<optional>"}. For outperforms: {"compared_to": "<entity name>", "on": "<task/benchmark>"}. For developed_by: {"lab": "<lab name>"}. For evaluated_on: {"benchmark": "<name>"}.
4. subject_name should be the canonical entity name as it appears in the paper (e.g. "GPT-4", "RoboAgent", "MMLU").
5. If no claims fit these predicates, return an empty claims list.
6. Do NOT invent claims not in the abstract.

=== EXAMPLE 1 ===
Title: "LLaMA 2: Open Foundation and Fine-Tuned Chat Models"
Abstract: "...Llama 2-Chat achieves 72.8% on MMLU, outperforming GPT-3 on multiple benchmarks. Llama 2 was developed by Meta AI..."

Output:
{
  "claims": [
    {
      "predicate": "achieves_score",
      "subject_name": "Llama 2-Chat",
      "object": {"score": 72.8, "benchmark": "MMLU", "metric": "accuracy"},
      "raw_excerpt": "Llama 2-Chat achieves 72.8% on MMLU",
      "confidence": 0.95
    },
    {
      "predicate": "outperforms",
      "subject_name": "Llama 2-Chat",
      "object": {"compared_to": "GPT-3", "on": "multiple benchmarks"},
      "raw_excerpt": "outperforming GPT-3 on multiple benchmarks",
      "confidence": 0.85
    },
    {
      "predicate": "developed_by",
      "subject_name": "Llama 2",
      "object": {"lab": "Meta AI"},
      "raw_excerpt": "Llama 2 was developed by Meta AI",
      "confidence": 0.99
    }
  ]
}

=== EXAMPLE 2 ===
Title: "GR00T N1: A Generalist Robot Policy"
Abstract: "...GR00T N1 is evaluated on 50 manipulation tasks from RoboArena and achieves a 78% task success rate, outperforming prior single-task specialists..."

Output:
{
  "claims": [
    {
      "predicate": "achieves_score",
      "subject_name": "GR00T N1",
      "object": {"score": 78.0, "benchmark": "RoboArena", "metric": "task success rate"},
      "raw_excerpt": "achieves a 78% task success rate",
      "confidence": 0.92
    },
    {
      "predicate": "evaluated_on",
      "subject_name": "GR00T N1",
      "object": {"benchmark": "RoboArena"},
      "raw_excerpt": "evaluated on 50 manipulation tasks from RoboArena",
      "confidence": 0.99
    },
    {
      "predicate": "outperforms",
      "subject_name": "GR00T N1",
      "object": {"compared_to": "prior single-task specialists", "on": "RoboArena"},
      "raw_excerpt": "outperforming prior single-task specialists",
      "confidence": 0.88
    }
  ]
}

Now extract claims from the following paper. Return only valid JSON matching the schema.
"""

CLAIM_EXTRACTION_USER = """\
Title: {title}

Abstract: {abstract}
"""


def format_extraction_user(title: str, abstract: str) -> str:
    return CLAIM_EXTRACTION_USER.format(title=title, abstract=abstract)
