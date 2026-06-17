import asyncio
import os
import asyncpg
from dotenv import load_dotenv

load_dotenv('.env')

async def main():
    conn = await asyncpg.connect(os.getenv('DATABASE_URL'))
    rows = await conn.fetch("SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = 'public' AND table_name IN ('usuarios', 'negocios')")
    for r in rows:
        print(f"{r['table_name']} | {r['column_name']} | {r['data_type']}")
    await conn.close()

if __name__ == '__main__':
    asyncio.run(main())
