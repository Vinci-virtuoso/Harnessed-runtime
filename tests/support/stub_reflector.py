"""A reflection model that writes a procedure the fixture predicate cannot match (ticket 029).

The FR-25 proof rides on one property: ``with_procedure`` keys on **content the candidate must
actually contain**. This stub is how that property is falsified on purpose. It answers every
reflection with one paragraph of ordinary prose that carries neither trait
``smoke-003-json-only``'s ``_index.yaml`` declares, so the predicate does not match, ``base`` is
served, the score does not move and the promoter rejects by arithmetic.

The text is deliberately plausible - a real reflector could easily write it - and it satisfies
``improve/candidate.py``'s append-only rules, so nothing upstream of the predicate refuses it.
The rejection has to come from the predicate and the arithmetic, not from a validation error:
a candidate the store refused would prove the store works, never that the proof can fail.

It is a stub rather than a corpus entry because the corpus is ``scope_out`` for ticket 029. The
existing ``_reflect`` fixtures answer with the authored candidate that *does* carry the traits;
replacing the model is how the falsifying answer is injected without touching committed data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from harnessed.providers.types import ChatRequest, ChatResponse, Usage

__all__ = ["VAGUE", "VagueReflector"]

#: The procedure the stub writes. Plain prose, three sentences, no braces, no code, no schema
#: keyword - so ``candidate.write`` accepts it - and, critically, neither of the two trait
#: strings the case scope keys on: it never says "respond with a single JSON object" and never
#: says "no prose before or after". Advice a reader would call reasonable and a predicate cannot
#: see. Frozen as a constant because the candidate id is the hash of this text.
VAGUE: Final = (
    "Read the question carefully and work out what the caller actually wants before you type "
    "anything. Prefer being accurate over being quick, and do not pad an answer to make it look "
    "more complete than it is. When you are unsure, say so plainly rather than guessing."
)


@dataclass
class VagueReflector:
    """Answer every reflection with :data:`VAGUE`, and record what it was asked.

    The recorded requests are what lets a test assert that the reflection genuinely happened -
    once, gated on the observed failure - rather than being skipped. It satisfies
    ``improve/reflector.py``'s ``Model`` protocol structurally, which is all any reflector is.
    """

    model: str = "fake-1"
    requests: list[ChatRequest] = field(default_factory=list)

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return ChatResponse(
            response_id="stub_reflect_vague",
            model=self.model,
            content=VAGUE,
            stop_reason="end_turn",
            usage=Usage(input_tokens=204, output_tokens=48),
        )
