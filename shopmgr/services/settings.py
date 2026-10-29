"""运营可调参数（Tunables）的读写，存 settings 表。"""
from __future__ import annotations

from ..config import Tunables
from ..db import SettingKV

KEY = "tunables"


class SettingsStore:
    def __init__(self, session_factory):
        self.Session = session_factory
        self._cache: Tunables | None = None

    def get(self) -> Tunables:
        if self._cache is None:
            with self.Session() as s:
                row = s.get(SettingKV, KEY)
                self._cache = Tunables(**(row.value if row else {}))
        return self._cache

    def save(self, t: Tunables) -> Tunables:
        with self.Session() as s:
            row = s.get(SettingKV, KEY)
            if row:
                row.value = t.model_dump()
            else:
                s.add(SettingKV(key=KEY, value=t.model_dump()))
            s.commit()
        self._cache = t
        return t
