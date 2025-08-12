# projectwise/services/workflow/intent_clasification.py
import asyncio
from typing import Literal
from pydantic import BaseModel
from openai import AsyncOpenAI
from quart import current_app


class IntentClassification(BaseModel):
    intent: Literal[
        "analisis_proyek", "generate_proposal", "websearch", "hitung_harga", "other"
    ]
    confidence: float
    reasoning: str


class AsyncIntentClassifier:
    """
    Async class untuk melakukan klasifikasi intent user.
    Menggunakan OpenAI responses API dengan model GPT-4o.
    """

    def __init__(self):
        service_configs=current_app.extensions["service_configs"]
        self.client = AsyncOpenAI()
        self.model = service_configs.llm_model

    async def classify(self, user_input: str) -> IntentClassification:
        """
        Mengklasifikasikan input user menjadi:
        - generate_proposal
        - analisis_proyek
        - other
        """
        response = await self.client.responses.parse(
            model=self.model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Classify the user's input into one of the following intents:\n"
                        "1. generate_proposal — if the user is asking to create, draft, or prepare a proposal.\n"
                        "2. analisis_proyek — if the user is asking for analysis, review, or evaluation of a project.\n"
                        "3. other — if the request does not match the first two categories.\n"
                        "Return your reasoning, and a confidence score between 0.0 and 1.0."
                    ),
                },
                {"role": "user", "content": user_input},
            ],
            text_format=IntentClassification,
        )
        return response.output_parsed  # type: ignore

    async def route(self, user_input: str) -> tuple[str, IntentClassification]:
        """
        Memproses routing berdasarkan intent yang terdeteksi.
        """
        classification = await self.classify(user_input)

        if classification.intent == "generate_proposal":
            result = await self.handle_generate_proposal(user_input)
        elif classification.intent == "analisis_proyek":
            result = await self.handle_analisis_proyek(user_input)
        else:
            result = await self.handle_other(user_input)

        return result, classification

    async def handle_generate_proposal(self, text: str) -> str:
        await asyncio.sleep(0)  # simulasi async
        return f"📄 Generating proposal based on: {text}"

    async def handle_analisis_proyek(self, text: str) -> str:
        await asyncio.sleep(0)  # simulasi async
        return f"📊 Analyzing project based on: {text}"

    async def handle_other(self, text: str) -> str:
        await asyncio.sleep(0)  # simulasi async
        return f"🤔 This doesn't match the known intents. Input was: {text}"


# ==== Contoh penggunaan ====
async def main():
    classifier = AsyncIntentClassifier()

    test_inputs = [
        "Buatkan proposal kerjasama untuk proyek energi terbarukan",
        "Tolong analisis risiko dari proyek konstruksi ini",
        "Kapan deadline pengumpulan laporan?",
    ]

    for user_input in test_inputs:
        result, classification = await classifier.route(user_input)
        print(f"\nInput: {user_input}")
        print(
            f"Intent: {classification.intent} (confidence: {classification.confidence:.2f})"
        )
        print(f"Reasoning: {classification.reasoning}")
        print(f"Response: {result}")


if __name__ == "__main__":
    asyncio.run(main())
