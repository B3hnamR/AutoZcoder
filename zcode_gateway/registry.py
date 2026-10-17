"""Coding-plan models observed in the ZCode 3.14.4 captures."""
CAPTURED_MODEL_VARIANTS = {'GLM-5.3': 'anthropic', 'GLM-5.3-Flash': 'anthropic'}
CATALOG_FREE_MODELS = frozenset(CAPTURED_MODEL_VARIANTS)

class Registry:
    def __init__(self, aliases=None, default='GLM-5.3-Flash', default_anthropic='GLM-5.3', probe_unknown=False):
        self.aliases = dict(aliases or {})
        self.default, self.default_anthropic = default, default_anthropic
        self.probe_unknown = False
        self.free_models = CATALOG_FREE_MODELS
    def resolve(self, requested, dialect='openai'):
        model = requested or (self.default_anthropic if dialect == 'anthropic' else self.default)
        model = self.aliases.get(model, model)
        return next((m for m in CAPTURED_MODEL_VARIANTS if m.lower() == model.lower()), model)
    def knows(self, model):
        return self.resolve(model) in CAPTURED_MODEL_VARIANTS
    def variant_for(self, model): return 'anthropic'
    def learn(self, model, variant): pass
    def lane_for(self, model): return 'free'
    def is_free_model(self, model): return self.knows(model)
    @staticmethod
    def is_free(model): return model in CATALOG_FREE_MODELS
    def catalogue(self):
        out = [{'id': m, 'name': m, 'object': 'model', 'owned_by': 'z.ai',
                'zcode_variant': 'anthropic', 'captured': True, 'is_free': True,
                'catalog_group': 'coding_plan', 'context_window': 1000000,
                'max_output_tokens': 128000, 'reasoning': True,
                'reasoning_efforts': ['low', 'high', 'max']}
               for m in CAPTURED_MODEL_VARIANTS]
        canonical_ids = {m.lower() for m in CAPTURED_MODEL_VARIANTS}
        out.extend({'id': alias, 'object': 'model', 'owned_by': 'z.ai', 'alias_of': target,
                    'zcode_variant': 'anthropic', 'is_free': True}
                   for alias, target in self.aliases.items() if alias.lower() not in canonical_ids)
        return out

def model_lane(model, free_models=None): return 'free'
