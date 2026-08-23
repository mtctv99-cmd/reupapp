import asyncio
import os
import sys

# ponytail: simple CLI wrapper to test pipeline end-to-end bypassing GUI/API
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline.orchestrator import PipelineOrchestrator
from core.config import settings

async def main():
    url = "https://www.bilibili.com/video/BV1767g6qEuz/"
    
    # Configure API keys if needed (using test keys or env vars)
    print(f"Starting test for: {url}")
    
    orchestrator = PipelineOrchestrator(
        source_url=url,
        source_lang="zh",
        target_lang="vi",
        voice="BV074_streaming"
    )
    
    result = await orchestrator.run()
    print("\n--- RESULT ---")
    print(f"Status: {result.get('status')}")
    print(f"Error: {result.get('error')}")
    if result.get('status') == 'success':
        print(f"Output saved to: {result.get('result_path')}")
    
if __name__ == "__main__":
    asyncio.run(main())
