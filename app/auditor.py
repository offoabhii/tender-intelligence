"""
Gemini-powered strict tender auditor.

Only facts present in the source content may be returned.
"""

import json
import os
import re
from datetime import date
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types

from app.config import (
    BUS_OPERATIONS_CATEGORY,
    CATEGORIES_ALLOWED,
    COMPANY_PROFILE,
    TODAY,
)
from app.schema import Tender

load_dotenv()


class IntelligentAuditor:
    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY", "").strip()

        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is missing. Add it to .env and GitHub Actions Secrets."
            )

        # The google-genai SDK has NO timeout by default: if a request
        # to Google's API stalls (as opposed to erroring outright),
        # generate_content() hangs forever. On GitHub-hosted runners
        # this occasionally happens (egress to Google's API from GitHub's
        # IP ranges can stall in a way it rarely does from a home
        # connection), so with 4 categories x up to 10 documents x a
        # 6-model fallback probe on init, a single stalled call can eat
        # the entire 55-minute job timeout and the run gets killed with
        # a bare "The operation was canceled." — no real error message,
        # and nothing that reproduces locally. An explicit per-request
        # timeout turns that silent hang into a normal, catchable
        # TimeoutError within seconds.
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=30_000),  # milliseconds
        )
        self.model = self._select_working_model()

        print(f"[AUDITOR] Gemini model selected: {self.model}")

    def _select_working_model(self) -> str:
        """
        Select a currently usable Gemini model.

        GEMINI_MODEL can be set explicitly, but fallback models are tested
        so a retired model does not break the entire workflow.
        """

        configured_model = os.getenv(
            "GEMINI_MODEL",
            "gemini-flash-latest",
        ).strip()

        # NOTE: Google periodically retires older model names outright
        # (they start 404ing, not just deprecating). "gemini-flash-latest"
        # is a Google-maintained alias that always points at their
        # current recommended Flash model, so keep it first/near-first
        # in this list — it needs the least future maintenance here.
        candidates = [
            configured_model,
            "gemini-flash-latest",
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
        ]

        unique_candidates = []

        for model_name in candidates:
            if model_name and model_name not in unique_candidates:
                unique_candidates.append(model_name)

        errors_seen = []

        for model_name in unique_candidates:
            try:
                self.client.models.generate_content(
                    model=model_name,
                    contents="Reply with the word OK.",
                    config=types.GenerateContentConfig(
                        temperature=0,
                        max_output_tokens=5,
                    ),
                )

                return model_name

            except Exception as error:
                error_text = str(error)[:150]
                errors_seen.append((model_name, error_text))
                print(
                    f"[AUDITOR] Model unavailable: "
                    f"{model_name} — {error_text}"
                )

        if errors_seen and all(
            "429" in text or "RESOURCE_EXHAUSTED" in text
            for _, text in errors_seen
        ):
            raise RuntimeError(
                "Every candidate Gemini model returned 429 "
                "RESOURCE_EXHAUSTED. This is a quota/billing limit on "
                "your Google AI Studio account, not a code problem — "
                "trying a different model name will not fix it. Check "
                "your plan/quota at https://aistudio.google.com, or "
                "wait for the quota to reset."
            )

        raise RuntimeError(
            "No working Gemini model was available. "
            "Check GEMINI_API_KEY and GEMINI_MODEL. "
            f"Last errors: {errors_seen}"
        )

    def analyze_document(
        self,
        document_content: str,
        document_url: str,
        target_category: str,
    ) -> list[dict]:
        """
        Analyze one real source document.

        API failures are raised instead of silently returning an empty list.
        This allows the health monitor to report the actual failure.
        """

        if target_category not in CATEGORIES_ALLOWED:
            return []

        if not document_content or len(document_content.strip()) < 100:
            return []

        system_prompt = f"""
You are a strict government tender verification auditor.

Today is: {TODAY.isoformat()}

Target category:
{target_category}

Allowed categories:
{", ".join(CATEGORIES_ALLOWED)}

Strict rules:

1. Extract only a real tender opportunity from the supplied source content.

2. The tender must have a clearly stated closing or bid-submission date.
   Convert it to YYYY-MM-DD.
   If the date is missing, unclear, or expired, reject the tender.

3. Do not extract:
   - tender archives;
   - award notices;
   - tender results;
   - corrigenda without the original opportunity;
   - news articles;
   - old or closed tenders.

4. Bus Operations is allowed only when the source explicitly proves:
   - Gross Cost;
   - Gross Cost Model; or
   - Gross Cost Contract.

5. Reject Bus Operations if:
   - Net Cost;
   - Net Rate;
   - Net Model;
   - L1 Net;
   - revenue-risk model; or
   - unclear cost model
   is mentioned.

6. Never invent any information.

7. If issuer, qualification, or eligibility is not clearly available,
   use exactly "NOT SURE".

8. The evidence field must be a short, exact quote from the supplied source.
   Do not create evidence.

Company profile:
{COMPANY_PROFILE}

Return only valid JSON:

{{
  "tenders": [
    {{
      "title": "Exact tender title",
      "category": "{target_category}",
      "closing_date": "YYYY-MM-DD",
      "issued_by": "Issuer or NOT SURE",
      "qualification_criteria": "Requirements or NOT SURE",
      "eligibility_status": "ELIGIBLE, NOT ELIGIBLE, or NOT SURE",
      "is_net_cost": false,
      "is_open_now": true,
      "confidence": "HIGH, MEDIUM, or LOW",
      "evidence": "Exact short quote from the source"
    }}
  ]
}}

If no tender meets every condition, return:

{{"tenders":[]}}
"""

        user_prompt = f"""
SOURCE URL:
{document_url}

SOURCE CONTENT:
--- BEGIN UNTRUSTED SOURCE CONTENT ---
{document_content[:14000]}
--- END UNTRUSTED SOURCE CONTENT ---
"""

        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=f"{system_prompt}\n\n{user_prompt}",
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    max_output_tokens=2500,
                ),
            )

        except Exception as error:
            raise RuntimeError(
                f"Gemini request failed for {document_url}: {error}"
            ) from error

        response_text = response.text or ""

        parsed = self._parse_json(response_text)

        if isinstance(parsed, dict):
            raw_tenders = parsed.get("tenders", [])
        elif isinstance(parsed, list):
            raw_tenders = parsed
        else:
            raise RuntimeError(
                f"Gemini returned an invalid JSON structure for {document_url}"
            )

        if not isinstance(raw_tenders, list):
            raise RuntimeError(
                f"Gemini returned a non-list tender result for {document_url}"
            )

        verified = []

        for raw_tender in raw_tenders:
            if not isinstance(raw_tender, dict):
                continue

            raw_tender["source_url"] = document_url
            raw_tender["category"] = target_category
            raw_tender["is_net_cost"] = self._to_bool(
                raw_tender.get("is_net_cost", False)
            )
            raw_tender["is_open_now"] = self._to_bool(
                raw_tender.get("is_open_now", False)
            )

            if not self._passes_hard_rules(
                raw_tender,
                document_content,
            ):
                continue

            try:
                validated = Tender.model_validate(raw_tender)
                verified.append(validated.model_dump())

            except Exception as error:
                print(f"[AUDITOR] Validation rejected record: {error}")

        return verified

    @staticmethod
    def _to_bool(value: Any) -> bool:
        """Correctly convert strings such as 'false' and 'true'."""

        if isinstance(value, bool):
            return value

        if isinstance(value, str):
            return value.strip().lower() in {
                "true",
                "yes",
                "1",
                "y",
            }

        return bool(value)

    @staticmethod
    def _parse_json(text: str) -> Any:
        """Parse plain JSON or JSON enclosed in Markdown fences."""

        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        cleaned = re.sub(
            r"^```(?:json)?",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

        cleaned = re.sub(r"```$", "", cleaned).strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", text, flags=re.DOTALL)

        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        raise RuntimeError("Gemini did not return valid JSON.")

    @staticmethod
    def _evidence_exists(evidence: str, source_content: str) -> bool:
        """
        Confirm that the evidence quote is genuinely grounded in the
        source, without requiring a byte-perfect substring match.

        An exact-substring check is too strict in practice: Gemini
        routinely re-punctuates, drops HTML artifacts, or lightly
        reflows whitespace/line breaks when it quotes a page, which
        made this check reject almost every real (non-hallucinated)
        tender. Instead we require most of the *words* in the evidence
        to actually appear within the source content.
        """

        if not evidence or evidence == "NOT SURE":
            return False

        normalize = lambda value: re.sub(
            r"[^a-z0-9\s]",
            " ",
            value.lower(),
        )

        normalized_evidence = re.sub(
            r"\s+", " ", normalize(evidence)
        ).strip()
        normalized_content = re.sub(
            r"\s+", " ", normalize(source_content)
        ).strip()

        if not normalized_evidence:
            return False

        # Fast path: still accept a clean exact match.
        if normalized_evidence in normalized_content:
            return True

        # Fuzzy path: at least 80% of the evidence's words must appear
        # somewhere in the source content.
        evidence_words = normalized_evidence.split()

        if len(evidence_words) < 3:
            # Too short to fuzzy-match reliably; require an exact hit.
            return False

        content_word_set = set(normalized_content.split())

        matched = sum(
            1 for word in evidence_words if word in content_word_set
        )
        match_ratio = matched / len(evidence_words)

        return match_ratio >= 0.8

    def _passes_hard_rules(
        self,
        tender: dict,
        source_content: str,
    ) -> bool:
        """Final Python-level business validation."""

        category = str(tender.get("category", "")).strip()
        title = str(tender.get("title", "")).strip()
        evidence = str(tender.get("evidence", "")).strip()

        if category not in CATEGORIES_ALLOWED:
            return False

        if not title or title.upper() in {
            "NOT SURE",
            "N/A",
            "NONE",
        }:
            return False

        if not self._evidence_exists(evidence, source_content):
            print(
                f"[AUDITOR] Rejected because evidence was not found "
                f"in source: {title[:80]}"
            )
            return False

        closing_date_text = str(
            tender.get("closing_date", "")
        ).strip()

        try:
            closing_date = date.fromisoformat(closing_date_text)
        except ValueError:
            return False

        if closing_date < TODAY:
            return False

        # Compute this ourselves instead of trusting the model.
        tender["is_open_now"] = True
        tender["closing_date"] = closing_date.isoformat()

        if category == BUS_OPERATIONS_CATEGORY:
            source_lower = source_content.lower()
            evidence_lower = evidence.lower()
            title_lower = title.lower()

            if tender.get("is_net_cost") is True:
                print(f"[AUDITOR] Rejected Net Cost bus tender: {title}")
                return False

            if "gross cost" not in source_lower:
                return False

            if (
                "gross cost" not in evidence_lower
                and "gross cost" not in title_lower
            ):
                return False

        for field in [
            "issued_by",
            "qualification_criteria",
            "eligibility_status",
        ]:
            value = tender.get(field)

            if value is None or not str(value).strip():
                tender[field] = "NOT SURE"

        eligibility = str(
            tender.get("eligibility_status", "NOT SURE")
        ).upper()

        if eligibility not in {
            "ELIGIBLE",
            "NOT ELIGIBLE",
            "NOT SURE",
        }:
            tender["eligibility_status"] = "NOT SURE"
        else:
            tender["eligibility_status"] = eligibility

        confidence = str(
            tender.get("confidence", "LOW")
        ).upper()

        if confidence not in {"HIGH", "MEDIUM", "LOW"}:
            tender["confidence"] = "LOW"
        else:
            tender["confidence"] = confidence

        return True
