import asyncio
from src.agent import run_agent

if __name__ == "__main__":
    print("=== Universal AI Web Scraping Agent ===")
    target_url = input("Paste Website URL: ").strip()
    user_prompt = input("What do you want to extract? ").strip()
    csv_file = input("Output CSV filename (saved in 'data/', Enter for 'output.csv'): ").strip() or "output.csv"

    asyncio.run(run_agent(target_url, user_prompt, csv_file))