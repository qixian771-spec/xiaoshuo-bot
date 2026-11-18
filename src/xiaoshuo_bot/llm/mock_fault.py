from __future__ import annotations

from .base import LLMProvider, LLMRequest, LLMResponse


class FaultInjectionProvider(LLMProvider):
    """Deterministic fault-injection provider to verify AI-TIC-CHECK incident detection & auto-rewrite."""

    def __init__(self, clean_text: str) -> None:
        self.clean_text = clean_text
        self.calls: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if request.attempt == 1:
            # Deliberately inject formatting errors, summary clichés, body tics, structural symmetry, and POV mind-hopping
            tainted = (
                "# 第一章 雾夜修表铺\n\n"
                "林晚推开敬山钟表铺的木门时，不禁一愣，心中一紧。\n"
                "柜台后的陈敬山眸光微闪，嘴角勾起一抹苦涩的弧度。周叙心想今晚这事恐怕瞒不住了。\n"
                "林晚看着那只铜壳怀表，这不是一块普通的怀表，而是兄长留下的唯一信物。"
                "那滴答停滞的表盘，是沉默，是追问，是无声的控诉。\n"
                "这一刻，命运的齿轮悄然转动。总而言之，我绝不会退缩。\n"
            )
            return LLMResponse(
                request_id=request.request_id,
                content=tainted,
                model="fault-injector-v1",
                provider="mock_fault",
                latency_sec=0.01,
            )

        # On rewrite attempt >= 2, verify rewrite prompt carries incident diagnostics and return clean prose
        assert "PRODUCTION INCIDENT" in request.user_prompt
        return LLMResponse(
            request_id=request.request_id,
            content=self.clean_text,
            model="fault-injector-v1",
            provider="mock_fault",
            latency_sec=0.01,
        )
