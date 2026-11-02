"""Commands/CommandVision.py - 後方互換の再公開口.

実体は pokecon/domain/CommandVision.py へ移った。新規のコードは core 側から読むこと。
"""

from pokecon.domain.CommandVision import (
    IMREAD_CACHE_SIZE as IMREAD_CACHE_SIZE,
    TEMPLATE_PATH as TEMPLATE_PATH,
    VisionMixin as VisionMixin,
    _get_template_filespec as _get_template_filespec,
    _imread_or_raise as _imread_or_raise,
    clear_template_cache as clear_template_cache,
)
