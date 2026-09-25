import json

from aiogram.fsm.storage.base import BaseStorage, StorageKey

from database import get_db
from models import FSMState


class SQLiteStorage(BaseStorage):
    """Persist aiogram FSM state in the application's SQLite database."""

    async def set_state(self, key: StorageKey, state=None) -> None:
        # aiogram ممکن است شیء State بدهد؛ storage باید رشته "Group:name" را ذخیره کند
        if state is not None and not isinstance(state, str):
            state = state.state
        with get_db() as db:
            row = db.query(FSMState).filter(FSMState.key == self._key(key)).first()
            if state is None:
                if row:
                    db.delete(row)
                return
            if not row:
                row = FSMState(key=self._key(key), state=str(state))
                db.add(row)
            else:
                row.state = str(state)

    async def get_state(self, key: StorageKey):
        with get_db() as db:
            row = db.query(FSMState).filter(FSMState.key == self._key(key)).first()
            return row.state if row else None

    async def set_data(self, key: StorageKey, data) -> None:
        with get_db() as db:
            row = db.query(FSMState).filter(FSMState.key == self._key(key)).first()
            if not row:
                row = FSMState(key=self._key(key), state=None)
                db.add(row)
            row.data = json.dumps(dict(data), ensure_ascii=False)

    async def get_data(self, key: StorageKey):
        with get_db() as db:
            row = db.query(FSMState).filter(FSMState.key == self._key(key)).first()
            if not row or not row.data:
                return {}
            return json.loads(row.data)

    async def update_data(self, key: StorageKey, data):
        current = await self.get_data(key)
        current.update(dict(data))
        await self.set_data(key, current)
        return current

    async def close(self) -> None:
        return None

    @staticmethod
    def _key(key: StorageKey) -> str:
        return ":".join(str(part) for part in (
            key.bot_id, key.chat_id, key.user_id, key.destiny,
        ))
