"""Optional adapter for the HU-xiaobai/xMemory public facade.

Importing this package does not import the third-party research library.  A
facade object is injected by the composition root instead.
"""

from .adapter import (
    XMEMORY_MODULE_ID,
    XMEMORY_MODULE_VERSION,
    XMemoryFacade,
    XMemoryModule,
    XMemoryRuntime,
    build_xmemory_runtime,
)

__all__ = [
    "XMEMORY_MODULE_ID",
    "XMEMORY_MODULE_VERSION",
    "XMemoryFacade",
    "XMemoryModule",
    "XMemoryRuntime",
    "build_xmemory_runtime",
]
