import asyncio
import time
from datetime import datetime
from src.agent import run_agent

if __name__ == "__main__":
    print("=== Universal AI Web Scraping Agent ===")
    target_url = input("Paste Website URL: ").strip()
    user_prompt = input("What do you want to extract? ").strip()
    csv_file = input("Output CSV filename (saved in 'data/', Enter for 'output.csv'): ").strip() or "output.csv"

    start_time = time.time()
    start_dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[Execution Started] {start_dt}")

    asyncio.run(run_agent(target_url, user_prompt, csv_file))

    end_time = time.time()
    end_dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elapsed = end_time - start_time
    minutes, seconds = divmod(elapsed, 60)

    print(f"\n[Execution Finished] {end_dt}")
    if minutes > 0:
        print(f"[Total Time Taken] {int(minutes)}m {seconds:.2f}s ({elapsed:.2f}s)")
    else:
        print(f"[Total Time Taken] {elapsed:.2f}s")
