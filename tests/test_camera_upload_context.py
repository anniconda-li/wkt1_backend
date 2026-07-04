"""Camera upload generation and ready-context tests."""

from __future__ import annotations

import asyncio
import os
import struct
import sys
import threading
import unittest
import wave
from io import BytesIO
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server.walkie_app as walkie_app
from server.walkie_app import create_http_app
from services.guide_answer_service import GuideAnswerResult
from services.vision_service import VisualDescription
from services.visual_match_service import VisualMatchResult


TEST_IMAGE = Path(__file__).resolve().parent / "data" / "camera" / "yingguo_yuying.jpg"


def make_wav() -> bytes:
    pcm = struct.pack("<" + "h" * 1600, *([0] * 1600))
    out = BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm)
    return out.getvalue()


class BlockingVisionService:
    """Fake vision service that blocks the first upload until released."""

    provider = "fake"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.first_started = threading.Event()
        self.release_first = threading.Event()

    def analyze_image(self, image_path: Path) -> VisualDescription:
        with self._lock:
            self.calls += 1
            call_no = self.calls
        if call_no == 1:
            self.first_started.set()
            if not self.release_first.wait(timeout=5):
                raise RuntimeError("first upload was not released")
        return VisualDescription(
            category="青铜器",
            visual_description=f"测试视觉描述 {image_path.stem}",
            shape_features=["球形主体", "带盖"],
            decoration_features=["环带纹饰"],
            color_material=["青铜"],
            search_keywords=["青铜器"],
            is_clear=True,
            confidence=0.92,
        )


class FakeVisualMatchService:
    async def match_async(self, desc: VisualDescription) -> VisualMatchResult:
        await asyncio.sleep(0)
        return VisualMatchResult(
            match_id="denggong_gui",
            match_name="邓公簋",
            confidence=0.88,
            evidence=desc.visual_description,
        )


class RecordingGuideAnswerService:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def build_answer_async(
        self,
        desc: VisualDescription,
        match: VisualMatchResult,
        *,
        user_question: str = "这是什么",
        device: str = "",
        image_id: str = "",
        conversation_context: str = "",
    ) -> GuideAnswerResult:
        self.calls.append(
            {
                "user_question": user_question,
                "device": device,
                "image_id": image_id,
                "conversation_context": conversation_context,
            }
        )
        return GuideAnswerResult(
            "specific_explain",
            True,
            f"测试回答：{user_question}",
            "test",
            match.match_id,
            match.match_name,
        )


class FailingVoiceQaService:
    async def _ask_llm_async(self, *args, **kwargs) -> str:
        raise AssertionError("contextual image follow-up should not call generic voice QA")


class CameraUploadContextTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["CAMERA_UPLOAD_TIMEOUT"] = "5"
        os.environ["AUTO_TTS_BACKGROUND"] = "false"
        os.environ["TOUR_MODE"] = "asr_bailian_app"
        self._old_transcribe = walkie_app.transcribe_wav
        root = Path("tmp/debug/test_camera_upload_context")
        self.app = create_http_app(root / "wav", root / "jpg", 1, False)
        self.vision = BlockingVisionService()
        self.app.state.vision_service = self.vision
        self.app.state.visual_match = FakeVisualMatchService()
        self.jpeg = TEST_IMAGE.read_bytes()
        self.wav = make_wav()

    def tearDown(self) -> None:
        walkie_app.transcribe_wav = self._old_transcribe

    async def _client(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    def test_later_upload_wins_even_if_first_finishes_later(self) -> None:
        async def scenario() -> None:
            async with await self._client() as client:
                first = asyncio.create_task(
                    client.post("/camera/upload?device=test-camera", content=self.jpeg, headers={"Content-Type": "image/jpeg"})
                )
                started = await asyncio.to_thread(self.vision.first_started.wait, 2)
                self.assertTrue(started)

                second = await client.post(
                    "/camera/upload?device=test-camera",
                    content=self.jpeg,
                    headers={"Content-Type": "image/jpeg"},
                )
                self.assertEqual(second.status_code, 200)
                second_data = second.json()
                self.assertTrue(second_data["ok"])
                self.assertEqual(second_data["status"], "ready")

                self.vision.release_first.set()
                first_response = await first
                self.assertEqual(first_response.status_code, 409)
                self.assertFalse(first_response.json()["ok"])

                cached = self.app.state.latest_visual_descriptions["test-camera"]
                self.assertEqual(cached["image_id"], second_data["image_id"])
                self.assertEqual(cached["status"], "ready")

        asyncio.run(scenario())

    def test_cancel_invalidates_pending_upload_before_next_ready_context(self) -> None:
        async def scenario() -> None:
            async with await self._client() as client:
                first = asyncio.create_task(
                    client.post("/camera/upload?device=test-camera", content=self.jpeg, headers={"Content-Type": "image/jpeg"})
                )
                started = await asyncio.to_thread(self.vision.first_started.wait, 2)
                self.assertTrue(started)

                cancel = await client.post("/camera/cancel?device=test-camera")
                self.assertEqual(cancel.status_code, 200)
                self.assertEqual(cancel.json()["status"], "canceled")

                self.vision.release_first.set()
                first_response = await first
                self.assertEqual(first_response.status_code, 409)

                second = await client.post(
                    "/camera/upload?device=test-camera",
                    content=self.jpeg,
                    headers={"Content-Type": "image/jpeg"},
                )
                self.assertEqual(second.status_code, 200)
                second_data = second.json()
                self.assertEqual(second_data["status"], "ready")

                cached = self.app.state.latest_visual_descriptions["test-camera"]
                self.assertEqual(cached["image_id"], second_data["image_id"])
                self.assertEqual(cached["generation"], second_data["generation"])

        asyncio.run(scenario())

    def test_pending_new_upload_hides_previous_ready_context(self) -> None:
        async def scenario() -> None:
            async with await self._client() as client:
                self.vision.release_first.set()
                first = await client.post(
                    "/camera/upload?device=test-camera",
                    content=self.jpeg,
                    headers={"Content-Type": "image/jpeg"},
                )
                self.assertEqual(first.status_code, 200)

                self.vision = BlockingVisionService()
                self.app.state.vision_service = self.vision
                second = asyncio.create_task(
                    client.post("/camera/upload?device=test-camera", content=self.jpeg, headers={"Content-Type": "image/jpeg"})
                )
                started = await asyncio.to_thread(self.vision.first_started.wait, 2)
                self.assertTrue(started)

                latest = await client.post("/camera/analyze_latest?device=test-camera")
                self.assertEqual(latest.status_code, 409)
                self.assertFalse(latest.json()["ok"])

                self.vision.release_first.set()
                second_response = await second
                self.assertEqual(second_response.status_code, 200)

        asyncio.run(scenario())

    def test_short_followup_uses_previous_image_answer_context(self) -> None:
        async def scenario() -> None:
            guide = RecordingGuideAnswerService()
            self.app.state.guide_answer_service = guide
            self.app.state.voice_qa_service = FailingVoiceQaService()
            walkie_app.transcribe_wav = lambda _path: "为什么"

            async with await self._client() as client:
                self.vision.release_first.set()
                upload = await client.post(
                    "/camera/upload?device=test-camera",
                    content=self.jpeg,
                    headers={"Content-Type": "image/jpeg"},
                )
                self.assertEqual(upload.status_code, 200)

                first_answer = await client.post("/camera/analyze_latest?device=test-camera")
                self.assertEqual(first_answer.status_code, 200)
                self.assertEqual(guide.calls[-1]["user_question"], "这是什么")

                start = await client.post("/ai/start", json={"device": "test-camera"})
                session = start.json()["session"]
                uploaded = await client.post(
                    f"/ai/upload?session={session}&index=0&offset=0&total={len(self.wav)}",
                    content=self.wav,
                )
                self.assertEqual(uploaded.status_code, 200)
                finish = await client.post(f"/ai/finish?session={session}", json={})
                self.assertEqual(finish.status_code, 200)

                followup_call = guide.calls[-1]
                self.assertIn("承接上一轮回答", followup_call["user_question"])
                self.assertIn("上一轮回答摘要", followup_call["conversation_context"])
                self.assertNotEqual(followup_call["user_question"], "为什么")

        asyncio.run(scenario())

    def test_pronoun_story_followup_uses_previous_image_answer_context(self) -> None:
        async def scenario() -> None:
            guide = RecordingGuideAnswerService()
            self.app.state.guide_answer_service = guide
            self.app.state.voice_qa_service = FailingVoiceQaService()
            walkie_app.transcribe_wav = lambda _path: "他有什么著名故事？"

            async with await self._client() as client:
                self.vision.release_first.set()
                upload = await client.post(
                    "/camera/upload?device=test-camera",
                    content=self.jpeg,
                    headers={"Content-Type": "image/jpeg"},
                )
                self.assertEqual(upload.status_code, 200)

                first_answer = await client.post("/camera/analyze_latest?device=test-camera")
                self.assertEqual(first_answer.status_code, 200)
                self.assertEqual(guide.calls[-1]["user_question"], "这是什么")

                start = await client.post("/ai/start", json={"device": "test-camera"})
                session = start.json()["session"]
                uploaded = await client.post(
                    f"/ai/upload?session={session}&index=0&offset=0&total={len(self.wav)}",
                    content=self.wav,
                )
                self.assertEqual(uploaded.status_code, 200)
                finish = await client.post(f"/ai/finish?session={session}", json={})
                self.assertEqual(finish.status_code, 200)

                followup_call = guide.calls[-1]
                self.assertIn("承接上一轮回答", followup_call["user_question"])
                self.assertIn("邓公簋", followup_call["user_question"])
                self.assertIn("著名故事", followup_call["user_question"])
                self.assertIn("上一轮回答摘要", followup_call["conversation_context"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
