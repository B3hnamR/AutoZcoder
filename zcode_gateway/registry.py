"""Coding-plan models observed in the ZCode captures."""
CAPTURED_MODEL_VARIANTS = {
    'GLM-5.3-Flash': 'anthropic',
    'GLM-5.4': 'anthropic',
}
UPSTREAM_MODEL_IDS = {'GLM-5.4': 'glm-5.4'}
CATALOG_FREE_MODELS = frozenset(CAPTURED_MODEL_VARIANTS)
DEFAULT_OPENAI_MODEL = 'GLM-5.3-Flash'
DEFAULT_ANTHROPIC_MODEL = 'GLM-5.4'


def is_retired_model(model: str) -> bool:
    # Recognize only the retired ID when upgrading existing user state.
    return model.strip().casefold() == 'glm-5.3'

# Captured 2026-11-23: GLM-5.4 accepted a PNG, a 131072 ceiling, and a
# 1048576 context window on the coding-plan endpoint.
MODEL_CAPS = {
    'GLM-5.4': {'vision': True, 'max_output_tokens': 131072,
                      'context_window': 1048576},
    'GLM-5.3-Flash': {'vision': True,  'max_output_tokens': 128000,
                      'context_window': 1048576},
}


def model_caps(model: str) -> dict:
    caps = MODEL_CAPS.get(model)
    if caps:
        return caps
    # unknown ids (aliases of the future): safe permissive defaults
    return {'vision': True, 'max_output_tokens': 131072, 'context_window': 1048576}


class Registry:
    def __init__(self, aliases=None, default=DEFAULT_OPENAI_MODEL,
                 default_anthropic=DEFAULT_ANTHROPIC_MODEL, probe_unknown=False):
        allowed_aliases = {
            alias: target for alias, target in (aliases or {}).items()
            if not is_retired_model(alias) and not is_retired_model(target)
        }
        dropped_aliases = (aliases or {}).keys() - allowed_aliases.keys()
        self.aliases = allowed_aliases
        self.default = (DEFAULT_ANTHROPIC_MODEL if is_retired_model(default)
                        or default in dropped_aliases else default)
        self.default_anthropic = (DEFAULT_ANTHROPIC_MODEL if is_retired_model(default_anthropic)
                                  or default_anthropic in dropped_aliases else default_anthropic)
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
