import asyncio
import time
from datetime import datetime
from src.agent import run_agent

if __name__ == "__main__":
    print("=== Universal AI Web Scraping Agent ===")
    target_url = input("Paste Website URL: ").strip().rstrip("\\")
    user_prompt = input("What do you want to extract? ").strip()
    csv_file = input("Output CSV filename (saved in 'data/', Enter for 'output.csv'): ").strip() or "output.csv"

    start_wall = datetime.now()
    start_perf = time.perf_counter()
    print(f"\n[Execution Started] {start_wall.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        asyncio.run(run_agent(target_url, user_prompt, csv_file))
    finally:
        end_wall = datetime.now()
        elapsed = time.perf_counter() - start_perf
        mins, secs = divmod(elapsed, 60)
        print(f"\n[Execution Finished] {end_wall.strftime('%Y-%m-%d %H:%M:%S')}")
        if mins >= 1:
            print(f"[Total Time Taken] {int(mins)}m {secs:.2f}s ({elapsed:.2f}s)")
        else:
            print(f"[Total Time Taken] {elapsed:.2f}s")