import asyncio
import sys
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
sys.stdout.reconfigure(encoding="utf-8")
from pipeline.orchestrator import PipelineOrchestrator

async def main():
    url = "https://www.bilibili.com/video/BV1RQtc6eEnh/"
    print("=== BẮT ĐẦU TEST E2E PIPELINE 8 BƯỚC ===")
    print(f"Target URL: {url}")

    async def progress_cb(step_idx, step_name, val):
        print(f"[Bước {step_idx}/8] {step_name}: {val*100:.0f}%")

    orc = PipelineOrchestrator(
        source_url=url,
        source_lang="zh",
        target_lang="vi",
        voice="ngoc_huyen",
        remove_sub=True,
        on_progress=progress_cb
    )

    result = await orc.run()
    out_path = result.get("output_path") or result.get("video_path")
    print("=== KẾT QUẢ JOB ===")
    print("Status:", result.get("status"))
    print("Output Path:", out_path)
    print("Title:", result.get("title"))
    assert result.get("status") == "done"
    assert out_path and os.path.exists(out_path)
    print("=== TEST E2E THÀNH CÔNG RỰC RỠ ===")

if __name__ == "__main__":
    asyncio.run(main())
