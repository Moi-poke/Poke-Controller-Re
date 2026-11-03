"""Commands/StickCommand.py - 後方互換の再公開口.

実体は pokecon/domain/StickCommand.py へ移った。新規のコードは core 側から読むこと。
"""

from pokecon.domain.StickCommand import (
    StickCommand as StickCommand,
    StickLeft as StickLeft,
    StickRight as StickRight,
)
