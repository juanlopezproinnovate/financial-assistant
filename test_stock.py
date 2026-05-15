import asyncio
from app.services.stock_service import stock_service

async def test():
    # negocio_id needs to be valid. The user's number is +51912345678 or something.
    # Let's just find the first business in the DB
    from app.database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        negocio = await conn.fetchrow("SELECT id FROM negocios LIMIT 1")
        if not negocio:
            print("No negocios found.")
            return
        nid = str(negocio["id"])
        
        # Test 1: Buscar candidatos
        candidatos = await stock_service.buscar_candidatos(nid, "polo")
        print("Candidatos:", candidatos)
        
        # Test 2: procesar_venta
        res = await stock_service.procesar_venta(nid, "polo", 2, 90.0)
        print("Procesar venta resultado:", res)

asyncio.run(test())
