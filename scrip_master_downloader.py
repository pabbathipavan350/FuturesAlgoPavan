_scrip_cache: list = []

def _get_scrip_master(client) -> list:
    global _scrip_cache
    if _scrip_cache:
        return _scrip_cache