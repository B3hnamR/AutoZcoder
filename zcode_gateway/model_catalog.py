"""Captured coding-plan catalog; chat.z.ai models belong to a different product."""
class ModelCatalog:
    def __init__(self, base_url, registry, **kwargs): self.registry = registry
    @property
    def status(self):
        return {'source': 'capture', 'updated_at': None, 'last_attempt_at': None,
                'error': None, 'app_version': '3.14.4'}
    async def refresh_if_due(self, *, force=False): return self.status
    async def aclose(self): pass
