from __future__ import annotations

import os
import re
import math
import json
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

# ----------------------------
# Logging
# ----------------------------
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('[%(levelname)s] %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


# ============================
# Section 1: Utilities
# ============================

_STOPWORDS = set("""a an the about above after again against all am an and any are aren't as at be because been before
being below between both but by can cannot could couldn't did didn't do does doesn't doing don't down during each few for
from further had hadn't has hasn't have haven't having he he'd he'll he's her here here's hers herself him himself his
how how's i i'd i'll i'm i've if in into is isn't it it's its itself let's me more most mustn't my myself no nor not of
off on once only or other ought our ours  ourselves out over own same shan't she she'd she'll she's should shouldn't so
some such than that that's the their theirs them themselves then there there's these they they'd they'll they're they've
this those through to too under until up very was wasn't we we'd we'll we're we've were weren't what what's when when's
where where's which while who who's whom why why's with won't would wouldn't you you'd you'll you're you've your yours
yourself yourselves""".split())


def _simple_tokenize(text: str) -> List[str]:
    # Lowercase, keep alphanumerics, split on non-letters
    text = text.lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    return [t for t in tokens if t and t not in _STOPWORDS]


# Optional stemming (uses nltk if available, otherwise identity)
try:
    from nltk.stem import PorterStemmer  # type: ignore
    _stemmer = PorterStemmer()

    def _stem(word: str) -> str:
        return _stemmer.stem(word)
except Exception:
    def _stem(word: str) -> str:
        return word


def _normalize_tokens(text: str) -> List[str]:
    return [_stem(tok) for tok in _simple_tokenize(text)]


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    A, B = set(a), set(b)
    if not A and not B:
        return 1.0
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def _cosine(u, v) -> float:
    num = float((u @ v).sum()) if hasattr(u, "@") else float(sum(ui * vi for ui, vi in zip(u, v)))
    du = math.sqrt(float((u * u).sum())) if hasattr(u, "sum") else math.sqrt(sum(ui * ui for ui in u))
    dv = math.sqrt(float((v * v).sum())) if hasattr(v, "sum") else math.sqrt(sum(vi * vi for vi in v))
    if du == 0 or dv == 0:
        return 0.0
    return num / (du * dv)


# ============================
# Section 2: Embedding Backends
# ============================

class EmbeddingBackend:
    """Abstract embedding backend"""
    def embed(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError


class SentenceTransformerBackend(EmbeddingBackend):
    def __init__(self, model_name: str = "sentence-transformers/all-mpnet-base-v2") -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except Exception as e:
            raise RuntimeError("sentence-transformers is not installed. Install it with `pip install sentence-transformers`.") from e
        self.model = SentenceTransformer(model_name)
        logger.info(f"Loaded sentence-transformers model: {model_name}")

    def embed(self, texts: List[str]) -> List[List[float]]:
        return self.model.encode(texts, show_progress_bar=False, normalize_embeddings=True).tolist()


class TfidfBackend(EmbeddingBackend):
    """Lightweight TF-IDF backend as a fallback when sentence-transformers isn't available."""
    def __init__(self) -> None:
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore
        except Exception as e:
            raise RuntimeError("scikit-learn is required for TF-IDF fallback. Install with `pip install scikit-learn`.") from e
        self.vectorizer = None
        self._TfidfVectorizer = TfidfVectorizer

    def embed(self, texts: List[str]) -> List[List[float]]:
        if self.vectorizer is None:
            self.vectorizer = self._TfidfVectorizer(lowercase=True, stop_words='english')
            X = self.vectorizer.fit_transform(texts)
        else:
            X = self.vectorizer.transform(texts)
        return X.toarray().tolist()


def get_default_backend(prefer_st: bool = True) -> EmbeddingBackend:
    if prefer_st:
        try:
            return SentenceTransformerBackend()
        except Exception as e:
            logger.warning(f"Falling back to TF-IDF backend because: {e}")
    return TfidfBackend()


# ============================
# Section 3: Markdown Parser
# ============================

@dataclass
class ParsedReport:
    failure_type: Optional[str] = None
    failure_description: Optional[str] = None
    important_parts_failure_description: List[str] = field(default_factory=list)
    explanation: Optional[str] = None
    recommendation: Optional[str] = None
    important_parts_recommendation: List[str] = field(default_factory=list)
    manual_recommendation: Optional[str] = None
    summary_counterfactual: Optional[str] = None
    causal_graph: Optional[str] = None


_HEADING_PATTERNS = {
    "failure_type": r"^\s*\*\*?Failure Type:?\*\*?\s*(?P<value>.+)$",
    "failure_description": r"^\s*\*\*?Failure Description:?\*\*?\s*(?P<value>.+)$",
    "important_parts_failure_description": r"^\s*\*\*?Important Parts of Failure Description:?\*\*?\s*(?P<value>.+)$",
    "explanation": r"^\s*\#*\s*\*\*?Explanation:?\*\*?\s*$",
    "recommendation": r"^\s*\#*\s*\*\*?Recommendation:?\*\*?\s*$",
    "important_parts_recommendation": r"^\s*\*\*?Important Parts of Recommendation:?\*\*?\s*(?P<value>.+)$",
    "manual_recommendation": r"^\s*\*\*?Recommendation from Scientific Literature:?\*\*?\s*(?P<value>.+)$",
    "summary_counterfactual": r"^\s*\*\*?Summary Counterfactual:?\*\*?\s*(?P<value>.+)$",
    "causal_graph": r"^\s*\*\*?Causal Graph:?\*\*?\s*(?P<value>.+)$",
}

def _split_list_field(raw: str) -> List[str]:
    """Split a line into 'important parts' items.
    Accepts comma- or semicolon-separated items and trims bullets if present.
    """
    if not raw:
        return []
    # Remove leading bullet text if present like "- " or "• "
    raw = re.sub(r"^[\-\*•]\s*", "", raw.strip())
    # Split on commas or semicolons
    parts = re.split(r",|;|\|", raw)
    parts = [p.strip(" -.•\t") for p in parts if p.strip()]
    return parts


def parse_markdown_report(md_path: str) -> ParsedReport:
    with open(md_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    parsed = ParsedReport()

    def capture_block(start_idx: int) -> Tuple[str, int]:
        """Capture a multi-line block (until the next bold/heading line)."""
        buf: List[str] = []
        i = start_idx + 1
        block_heading = re.compile(r"^\s*(\#|\*\*).+", re.IGNORECASE)
        while i < len(lines) and not block_heading.match(lines[i]):
            buf.append(lines[i])
            i += 1
        return "\n".join(buf).strip(), i - 1

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        matched = False
        for key, pat in _HEADING_PATTERNS.items():
            m = re.match(pat, line, flags=re.IGNORECASE)
            if m:
                matched = True
                if key in {"explanation", "recommendation"}:
                    val, new_i = capture_block(i)
                    setattr(parsed, key, val)
                    i = new_i
                else:
                    val = m.groupdict().get("value", "").strip()
                    if key == "important_parts_failure_description":
                        setattr(parsed, key, _split_list_field(val))
                    elif key == "important_parts_recommendation":
                        setattr(parsed, key, _split_list_field(val))
                    else:
                        setattr(parsed, key, val)
                break
        i += 1 if matched else 1

    return parsed


# ============================
# Section 4: Hybrid Two‑Tiered Coverage
# ============================

@dataclass
class CoverageMatch:
    concept: str
    matched: bool
    reason: str  # 'syntactic' or 'semantic' or 'none'
    score: float  # similarity score for semantic, 1.0 for syntactic, 0.0 otherwise
    span: Optional[str] = None  # optional text span evidence


@dataclass
class CoverageResult:
    coverage_percent: float
    matches: List[CoverageMatch]


class MetricsBasedEvaluator:
    def __init__(self, embedding_backend: Optional[EmbeddingBackend] = None, tau: float = 0.65) -> None:
        self.backend = embedding_backend or get_default_backend(prefer_st=True)
        self.tau = tau

    # --- Syntactic ---
    def _f_syn(self, concept: str, text: str) -> Tuple[bool, Optional[str]]:
        # literal case-insensitive
        if concept and concept.lower() in text.lower():
            return True, concept

        # stemmed keyword overlap
        c_toks = _normalize_tokens(concept)
        t_toks = _normalize_tokens(text)
        if c_toks:
            # condition 1: all concept tokens present in text tokens
            if set(c_toks).issubset(set(t_toks)):
                return True, "all tokens present"
            # condition 2: jaccard >= 0.5
            if _jaccard(c_toks, t_toks) >= 0.5:
                return True, f"jaccard>=0.5"
        return False, None

    # --- Semantic ---
    def _f_sem(self, concept: str, text: str) -> float:
        # compare to whole text and max over sentences for robustness
        sentences = re.split(r"(?<=[.!?])\s+", text.strip()) if text else []
        pool = [text] + sentences if text else [concept]
        embeddings = self.backend.embed([concept] + pool)
        c_vec = embeddings[0]
        sims = [_cosine(c_vec, v) for v in embeddings[1:]]
        return max(sims) if sims else 0.0

    def hybrid_two_tier_coverage(self, concepts: List[str], text: str) -> CoverageResult:
        matches: List[CoverageMatch] = []
        if not concepts:
            return CoverageResult(coverage_percent=100.0, matches=[])
        for c in concepts:
            syn_ok, syn_span = self._f_syn(c, text)
            if syn_ok:
                matches.append(CoverageMatch(concept=c, matched=True, reason="syntactic", score=1.0, span=syn_span))
                continue
            sem_score = self._f_sem(c, text)
            if sem_score >= self.tau:
                matches.append(CoverageMatch(concept=c, matched=True, reason="semantic", score=sem_score))
            else:
                matches.append(CoverageMatch(concept=c, matched=False, reason="none", score=sem_score))

        covered = sum(1 for m in matches if m.matched)
        coverage_percent = 100.0 * covered / max(1, len(concepts))
        return CoverageResult(coverage_percent=coverage_percent, matches=matches)


# ============================
# Section 5: LLM-as-Judge (optional external call)
# ============================

@dataclass
class LLMJudgeScore:
    criterion: str
    score: float  # 1-5
    rationale: str


@dataclass
class LLMJudgeResult:
    scores: List[LLMJudgeScore]
    overall: float


class LLMJudgeEvaluator:
    """LLM-as-Judge with two backends:
    1) OpenAI if OPENAI_API_KEY present (model configurable)
    2) A heuristic fallback using semantic similarity vs reference texts
    """
    def __init__(self, embedding_backend: Optional[EmbeddingBackend] = None, model: str = "gpt-4o-mini") -> None:
        self.backend = embedding_backend or get_default_backend(prefer_st=True)
        self.model = model

    def _heuristic_score(self, text: str, reference: str) -> float:
        if not text or not reference:
            return 1.0
        # semantic similarity in [0,1] -> map to 1..5
        sims = self.backend.embed([text, reference])
        sim = _cosine(sims[0], sims[1])
        # light bonus for syntactic overlap
        jacc = _jaccard(_normalize_tokens(text), _normalize_tokens(reference))
        blended = 0.7 * sim + 0.3 * jacc
        return max(1.0, min(5.0, 1.0 + 4.0 * blended))

    def judge_explanation_and_recommendation(
        self,
        explanation: str,
        recommendation: str,
        failure_description: str,
        manual_recommendation: str,
        summary_counterfactual: str,
        use_openai: bool = False,
        openai_model: Optional[str] = None,
    ) -> LLMJudgeResult:
        if use_openai and os.environ.get("OPENAI_API_KEY") is not None:
            try:
                import openai  # type: ignore
                client = openai.OpenAI()
                model = openai_model or self.model
                system = (
                    "You are a strict evaluator. Rate each criterion from 1 (poor) to 5 (excellent). "
                    "Provide very brief justification for each score."
                )
                prompt = f"""Text to evaluate:
---
EXPLANATION:
{explanation}

RECOMMENDATION:
{recommendation}

References:
- Failure Description: {failure_description}
- Manual Recommendation: {manual_recommendation}
- Summary Counterfactual: {summary_counterfactual}

Criteria:
1) How well does the EXPLANATION address the FAILURE DESCRIPTION?
2) How well does the RECOMMENDATION align with the MANUAL RECOMMENDATION?
3) How well do both incorporate the SUMMARY COUNTERFACTUAL insights?

Return JSON with keys: scores:[{{criterion, score, rationale}}], overall.
"""
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": prompt}],
                    temperature=0.0,
                )
                content = resp.choices[0].message.content
                try:
                    data = json.loads(content)
                except Exception:
                    # Try to extract JSON substring
                    m = re.search(r"\{[\s\S]*\}", content)
                    data = json.loads(m.group(0)) if m else {}
                scores = [LLMJudgeScore(**s) for s in data.get("scores", [])]
                overall = float(data.get("overall", sum(s.score for s in scores) / max(1, len(scores))))
                return LLMJudgeResult(scores=scores, overall=overall)
            except Exception as e:
                logger.warning(f"OpenAI evaluation failed, falling back to heuristic. Reason: {e}")

        # Heuristic fallback
        s1 = self._heuristic_score(explanation, failure_description)
        s2 = self._heuristic_score(recommendation, manual_recommendation)
        s3 = self._heuristic_score(explanation + "\n" + recommendation, summary_counterfactual)
        scores = [
            LLMJudgeScore("Explanation addresses failure description", s1, "semantic+overlap heuristic"),
            LLMJudgeScore("Recommendation aligns with manual recommendation", s2, "semantic+overlap heuristic"),
            LLMJudgeScore("Both incorporate counterfactual insights", s3, "semantic+overlap heuristic"),
        ]
        overall = sum(s.score for s in scores) / len(scores)
        return LLMJudgeResult(scores=scores, overall=overall)


# ============================
# Section 6: Comprehensive Evaluator
# ============================

@dataclass
class ComprehensiveResult:
    explanation_coverage: CoverageResult
    recommendation_coverage: CoverageResult
    llm_judge: LLMJudgeResult
    parsed: ParsedReport


class ComprehensiveEvaluator:
    def __init__(self, tau: float = 0.65, embedding_backend: Optional[EmbeddingBackend] = None) -> None:
        backend = embedding_backend or get_default_backend(prefer_st=True)
        self.metrics = MetricsBasedEvaluator(embedding_backend=backend, tau=tau)
        self.judge = LLMJudgeEvaluator(embedding_backend=backend)

    def evaluate_from_md(
        self,
        md_file_path: str,
        use_openai_for_judge: bool = False,
        openai_model: Optional[str] = None,
    ) -> ComprehensiveResult:
        parsed = parse_markdown_report(md_file_path)

        exp_cov = self.metrics.hybrid_two_tier_coverage(
            concepts=parsed.important_parts_failure_description,
            text=parsed.explanation or ""
        )
        rec_cov = self.metrics.hybrid_two_tier_coverage(
            concepts=parsed.important_parts_recommendation,
            text=parsed.recommendation or ""
        )
        judge_res = self.judge.judge_explanation_and_recommendation(
            explanation=parsed.explanation or "",
            recommendation=parsed.recommendation or "",
            failure_description=parsed.failure_description or "",
            manual_recommendation=parsed.manual_recommendation or "",
            summary_counterfactual=parsed.summary_counterfactual or "",
            use_openai=use_openai_for_judge,
            openai_model=openai_model,
        )
        return ComprehensiveResult(
            explanation_coverage=exp_cov,
            recommendation_coverage=rec_cov,
            llm_judge=judge_res,
            parsed=parsed,
        )


# ============================
# Section 7: CLI demo
# ============================


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Hybrid Two‑Tiered Coverage Evaluator")
    parser.add_argument("md_file", type=str, help="Path to the Markdown file to evaluate")
    parser.add_argument("--tau", type=float, default=0.65, help="Semantic similarity threshold")
    parser.add_argument("--openai", action="store_true", help="Use OpenAI for LLM-as-Judge if API key is set")
    parser.add_argument("--openai-model", type=str, default="gpt-4o-mini", help="OpenAI model name")
    args = parser.parse_args()

    evaluator = ComprehensiveEvaluator(tau=args.tau)
    result = evaluator.evaluate_from_md(args.md_file, use_openai_for_judge=args.openai, openai_model=args.openai_model)
    _pretty_print_result(result)