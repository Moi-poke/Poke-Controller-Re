"""Commands/CommandOperate.py - 後方互換の再公開口.

実体は pokecon/domain/CommandOperate.py へ移った。新規のコードは core 側から読むこと。
"""

from pokecon.domain.CommandOperate import (
    OperateMixin as OperateMixin,
    StopThread as StopThread,
)
