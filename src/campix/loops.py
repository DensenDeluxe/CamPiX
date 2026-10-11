def windows_selector():
    """Network-only asyncio loop; capture/FFmpeg processes use synchronous workers."""
    import asyncio

    return asyncio.SelectorEventLoop()
