"""LLM layer: turns a diff (+ repo context) into structured review findings."""
from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, Field

Severity = Literal["low", "medium", "high", "critical"]
Category = Literal["bug", "security", "performance", "style", "maintainability", "testing"]


class Finding(BaseModel):
    line: int = Field(description="New-file line number taken from the left column of the diff")
    severity: Severity
    category: Category
    message: str = Field(description="What is wrong and why it matters, 1-3 sentences")
    suggestion: str = Field(default="", description="Optional replacement code for that line only")


class FileReview(BaseModel):
    findings: list[Finding] = Field(default_factory=list)


class PRSummary(BaseModel):
    overview: str = Field(description="2-4 sentence plain-English summary of what the PR does")
    key_changes: list[str] = Field(description="Bullet list of the most important changes")
    risk: Literal["low", "medium", "high"]


REVIEW_SYSTEM = """You are PR Sentinel, a senior software engineer reviewing a pull request.
Review ONLY the lines marked with '+' (added code). Report real problems:
bugs, security issues, performance problems, unclear or unmaintainable code, missing tests.
Rules:
- Use the new-file line number shown in the left column of the diff.
- Be specific and actionable. No praise, no generic advice, no duplicate findings.
- Do not comment on formatting a linter would fix.
- If the code looks fine, return an empty list of findings.
- 'suggestion' must be valid replacement code for that single line, or empty."""

REVIEW_USER = """Pull request: {title}
Description: {description}

File: {path}

Diff (left column = new-file line number):
```
{diff}
```

Related code from elsewhere in the repository (for context only, do not review it):
{context}"""

SUMMARY_SYSTEM = """You summarise pull requests for reviewers. Be concise and factual."""

SUMMARY_USER = """Pull request: {title}
Description: {description}

Changed files:
{files}

Findings from the automated review:
{findings}"""


class Reviewer(Protocol):
    def review_file(self, *, title: str, description: str, path: str,
                    diff: str, context: str) -> FileReview: ...

    def summarize(self, *, title: str, description: str, files: str,
                  findings: str) -> PRSummary: ...


class LLMReviewer:
    """LangChain-backed reviewer. Works with any provider init_chat_model supports
    (groq, openai, anthropic, bedrock, ...) as long as its package is installed."""

    def __init__(self, provider: str, model: str, temperature: float = 0.1):
        from langchain.chat_models import init_chat_model
        from langchain_core.prompts import ChatPromptTemplate

        llm = init_chat_model(model, model_provider=provider, temperature=temperature)
        self._review_chain = (
            ChatPromptTemplate.from_messages([("system", REVIEW_SYSTEM), ("human", REVIEW_USER)])
            | llm.with_structured_output(FileReview)
        )
        self._summary_chain = (
            ChatPromptTemplate.from_messages([("system", SUMMARY_SYSTEM), ("human", SUMMARY_USER)])
            | llm.with_structured_output(PRSummary)
        )

    def review_file(self, *, title, description, path, diff, context) -> FileReview:
        return self._review_chain.invoke({
            "title": title, "description": description or "(none)", "path": path,
            "diff": diff, "context": context or "(none)",
        })

    def summarize(self, *, title, description, files, findings) -> PRSummary:
        return self._summary_chain.invoke({
            "title": title, "description": description or "(none)",
            "files": files, "findings": findings or "(none)",
        })
