"""Coding-plan models observed in the ZCode 3.14.4 captures."""
CAPTURED_MODEL_VARIANTS = {'GLM-5.3': 'anthropic', 'GLM-5.3-Flash': 'anthropic'}
CATALOG_FREE_MODELS = frozenset(CAPTURED_MODEL_VARIANTS)

# Per-model capabilities. Vision is confirmed for BOTH plan models (live-
# measured 2026-10-21 by the owner: Flash reads images natively; GLM-5.3
# also accepts image blocks — forwarding is ON by default per owner
# decision). If the upstream ever starts rejecting images on one of them,
# flip that entry to False to restore server-side stripping.
MODEL_CAPS = {
    'GLM-5.3':       {'vision': True,  'max_output_tokens': 128000,
                      'context_window': 1000000},
    'GLM-5.3-Flash': {'vision': True,  'max_output_tokens': 128000,
                      'context_window': 1000000},
}


def model_caps(model: str) -> dict:
    caps = MODEL_CAPS.get(model)
    if caps:
        return caps
    # unknown ids (aliases of the future): safe permissive defaults
    return {'vision': True, 'max_output_tokens': 128000, 'context_window': 1000000}


class Registry:
    def __init__(self, aliases=None, default='GLM-5.3-Flash', default_anthropic='GLM-5.3', probe_unknown=False):
        self.aliases = dict(aliases or {})
        self.default, self.default_anthropic = default, default_anthropic
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
    def supports_vision(self, model: str) -> bool:
        """Capability gate: can this model accept image blocks?"""
        return bool(MODEL_CAPS.get(model, {}).get('vision'))
    def max_output(self, model: str) -> int:
        return int(MODEL_CAPS.get(model, {}).get('max_output_tokens', 128000))
    def catalogue(self):
        out = [{'id': m, 'name': m, 'object': 'model', 'owned_by': 'z.ai',
                'zcode_variant': 'anthropic', 'captured': True, 'is_free': True,
                'catalog_group': 'coding_plan',
                'context_window': MODEL_CAPS[m]['context_window'],
                'max_output_tokens': MODEL_CAPS[m]['max_output_tokens'],
                'vision': MODEL_CAPS[m]['vision'],
                'reasoning': True,
                'reasoning_efforts': ['low', 'high', 'max']}
               for m in CAPTURED_MODEL_VARIANTS]
        canonical_ids = {m.lower() for m in CAPTURED_MODEL_VARIANTS}
        out.extend({'id': alias, 'object': 'model', 'owned_by': 'z.ai', 'alias_of': target,
                    'zcode_variant': 'anthropic', 'is_free': True}
                   for alias, target in self.aliases.items() if alias.lower() not in canonical_ids)
        return out

def model_lane(model, free_models=None): return 'free'
