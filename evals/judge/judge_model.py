"""The judge, wired to the operator's own configured provider. Built lazily, never at import.

``deepeval`` is an optional dependency under the ``[eval]`` extra. Every import of it in this file
is **inside a function**, which is what lets the rest of the suite collect on a base install where
the library is simply absent. An AST scan in ``deterministic/test_judge_tier.py`` keeps it that way.

The judge reuses whatever provider the operator has already configured rather than pinning a vendor
of its own. That is the same bet the rest of the harness makes: the seam is
``providers/types.py``, so a judge is just one more caller of ``complete``.

**A score from here is a percentage, not a truth.** Nothing under ``src/harnessed/`` imports this
module, and nothing may: a 0-1 score with a threshold cannot be allowed to gate a promotion.
"""

from __future__ import annotations

from typing import Any

from harnessed import config
from harnessed.providers import build_provider
from harnessed.providers.types import ChatRequest, Message

__all__ = ["JUDGE_MAX_TOKENS", "THRESHOLD", "available", "judge_model"]

#: The threshold every metric in this tier is declared against. Waku's number, kept: a judge that
#: passes everything measures nothing, and one that passes almost nothing measures the judge.
THRESHOLD = 0.6

#: The minimum distance a metric must put between a known-good output and a known-bad one before
#: this tier will believe it is measuring anything (ticket 043).
#:
#: **Why a distance and not another threshold.** A judge returning the same number for every input
#: satisfies any assertion of the form ``score < THRESHOLD``. That is not hypothetical: on
#: 2026-08-26 this tier scored a correct answer and a deliberately padded one identically at 0.0
#: and still reported a pass, because its only discriminating assertion was one-sided. A spread
#: cannot be faked by a constant, whatever the constant is.
#:
#: **Why 0.4.** A live judge's score moves between runs, so this has to clear noise without
#: demanding perfect separation. The observed spread on the reference pair was 1.0 against 0.0.
#: 0.4 leaves room for the good answer to drift down to 0.6 and the bad one up to 0.2 - both
#: further than anything yet seen - and still fails every constant. Raise it only with a
#: measurement to point at.
MARGIN = 0.4

#: A judge writes a short verdict and a reason, not an essay.
JUDGE_MAX_TOKENS = 1024


def available() -> bool:
    """Whether both gates on this tier are open: the library is installed and a key is configured.

    The import is attempted rather than assumed because the ``[eval]`` extra is optional by
    design, and ``ImportError`` is the honest way to find out.
    """
    try:
        import deepeval  # noqa: F401
    except ImportError:
        return False
    from evals.helpers import HAS_KEY

    return HAS_KEY


def judge_model() -> Any:
    """A ``DeepEvalBaseLLM`` backed by the harness's own provider seam.

    The subclass is defined **inside** this function on purpose: its base class is
    ``deepeval``'s, so a module-scope definition would make the import unconditional and the whole
    suite would stop collecting without the extra.
    """
    from deepeval.models import DeepEvalBaseLLM

    settings = config.load()
    provider = build_provider(settings)
    model_name = settings.model or settings.require_provider()

    class HarnessedJudge(DeepEvalBaseLLM):  # type: ignore[misc]
        """One ``complete`` call per judgement, through the same seam the loop uses."""

        def load_model(self) -> Any:
            return provider

        def generate(self, prompt: str, *args: Any, **kwargs: Any) -> str:
            response = provider.complete(
                ChatRequest(
                    model=model_name,
                    messages=(Message(role="user", content=prompt),),
                    max_tokens=JUDGE_MAX_TOKENS,
                )
            )
            return response.content

        async def a_generate(self, prompt: str, *args: Any, **kwargs: Any) -> str:
            return self.generate(prompt, *args, **kwargs)

        def get_model_name(self) -> str:
            return model_name

    return HarnessedJudge()
