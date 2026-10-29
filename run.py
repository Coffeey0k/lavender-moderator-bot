from dotenv import load_dotenv
load_dotenv()
from bot import main
import asyncio

if __name__ == "__main__":
    asyncio.run(main())
