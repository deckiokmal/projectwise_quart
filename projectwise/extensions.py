# projectwise/extensions.py
from quart import Quart
from .config import ServiceConfigs
from .utils.logger import get_logger
from .services.mcp.client import MCPClient
from .services.memory.short_term_memory import ShortTermMemory
from .services.memory.long_term_memory import Mem0Manager


mcp_client: MCPClient | None = None
service_configs: ServiceConfigs | None = None


async def init_extensions(app: Quart):
    """
    Inisialisasi semua ekstensi async, termasuk MCPClient.
    """
    global mcp_client, service_configs

    logger = get_logger(__name__)

    # Load service configs (dari .env)
    service_configs = ServiceConfigs()
    app.extensions["service_configs"] = service_configs
    logger.info(f"ServiceConfigs loaded: MCP URL = {service_configs.mcp_server_url}")

    # Buat dan koneksikan MCPClient
    mcp_client = await MCPClient(model=service_configs.llm_model).__aenter__()
    app.extensions["mcp"] = mcp_client
    logger.info(f"MCPClient connected to {service_configs.mcp_server_url}")

    # ShortTermMemory (pakai DB URL dari BaseConfig / app.config)
    db_url = app.config["SQLALCHEMY_DATABASE_URI"]
    short_term_memory = ShortTermMemory(db_url=db_url, echo=False, max_history=20)
    await short_term_memory.init_models()
    app.extensions["short_term_memory"] = short_term_memory
    logger.info(f"ShortTermMemory initialized with DB: {db_url}")

    # Long term memory
    long_term_memory = Mem0Manager(service_configs)
    await long_term_memory.init()
    app.extensions["long_term_memory"] = long_term_memory
    logger.info("LongTermMemory (Mem0Manager) initialized")


async def shutdown_extensions():
    """
    Tutup semua ekstensi async dengan aman.
    """
    global mcp_client
    logger = get_logger(__name__)

    if mcp_client:
        await mcp_client.__aexit__(None, None, None)
        logger.info("MCPClient disconnected")
        mcp_client = None
