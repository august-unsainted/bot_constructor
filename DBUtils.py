import sqlite3 as sq
from typing import Any

import pytz
from datetime import datetime
import locale

from aiogram.fsm.context import FSMContext

from BotConfig import BotConfig
from utils_funcs import find_resource_path


class DBUtils:
    def __init__(self, config: BotConfig):
        self.db = sq.connect(find_resource_path('data/applicant.db'))
        self.cur = self.db.cursor()
        self.__dict__.update({key: config.jsons[key] for key in ['keyboards', 'messages', 'stats']})
        self.tz = pytz.timezone('Asia/Irkutsk')
        locale.setlocale(category=locale.LC_ALL, locale="Russian")

    def get_stat_name(self, stat: str) -> str | None:
        for key, value in self.keyboards.items():
            for callback, text in value.items():
                if callback == stat:
                    return text
        return None

    def get_table_name(self) -> str:
        now = datetime.now(tz=self.tz)
        month, year = now.month, now.year
        return f'stats_{month}_{year}'

    async def start_db(self, *queries: list[str | list]):
        stats_table = self.get_table_name()
        self.cur.execute(f'''
                    CREATE TABLE IF NOT EXISTS {stats_table} (
                    button text PRIMARY KEY,
                    count INTEGER DEFAULT 0
                    )
                ''')
        self.cur.executemany(f'''
            INSERT OR IGNORE INTO {stats_table} (button)
            VALUES (?)
        ''', [(btn,) for btn in self.stats + ['active_users', 'inactive_users']])
        self.cur.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    is_active INTEGER DEFAULT 1
                    )
                ''')
        for query in queries:
            if query[0].strip().startswith('INSERT'):
                self.cur.executemany(query[0], query[1])
            else:
                self.cur.execute(query[0])
        self.db.commit()

    async def execute_query(self, query: str, *args: Any) -> None | list[tuple]:
        result = self.cur.execute(query, tuple(args))
        self.db.commit()
        if query.strip().startswith('SELECT'):
            return result.fetchall()
        return None

    async def add_user(self, user_id: int | str) -> None:
        await self.execute_query(
            '''
                INSERT INTO users (user_id)
                VALUES (?)
                ON CONFLICT(user_id)
                DO UPDATE SET is_active = 1;
            ''',
            str(user_id))

    async def count_users(self) -> dict[str, int]:
        result = {}
        table = self.get_table_name()
        for is_active in [1, 0]:
            key = f"{'in' * (not is_active)}active"
            result[key] = len(await self.execute_query('SELECT * FROM users WHERE is_active = ?', is_active))
            await self.execute_query(f"UPDATE {table} SET count = ? WHERE button = ?", result[key], key + '_users')

        result['all'] = sum(result.values())
        return result

    async def get_active_users(self) -> list[int]:
        results = await self.execute_query('SELECT user_id FROM users WHERE is_active = 1')
        return [result[0] for result in results]

    async def update_activity(self, user_id: int | str, activity: bool = False) -> None:
        await self.execute_query('UPDATE users SET is_active = ? WHERE user_id = ?;', int(activity), str(user_id))

    async def increase_stat_count(self, button: str) -> None:
        await self.execute_query(
            f'''
                UPDATE {self.get_table_name()}
                SET count = count + 1
                WHERE button = ?
            ''',
            button
        )

    async def get_stat_dict(self, table: str) -> dict[str, int]:
        table = table or self.get_table_name()
        entries = await self.execute_query(f'SELECT * FROM {table}')
        result = {}
        for entry in entries:
            btn = entry[0]
            result[self.get_stat_name(btn) or btn] = entry[1]
        return result

    async def get_stat(self, table: str = '', temp: dict[str, int] = None) -> tuple[int, Any, int | Any, str]:
        if temp is None:
            temp = {}
        table = await self.get_stat_dict(table)
        result, total, users = [], 0, []
        for text, count in table.items():
            if not text.endswith('users'):
                result.append(f'— «{text}»: {count}')
                total += count
            else:
                count -= temp.get(text) or 0
                users.append(count)
        return sum(users), *users, total, '\n'.join(result)

    async def get_stats(self) -> list[str]:
        template = self.messages.get('stat')
        main_text = self.messages.get('all_stat').format(*await self.get_stat())
        months = []
        temp = {}
        tables = await self.execute_query('SELECT name FROM sqlite_master WHERE type="table"')
        for table in tables:
            table = table[0]
            if not table.startswith('stats'):
                continue
            month_number, year = table.replace('stats_', '').split('_')
            month = datetime.strptime(month_number.rjust(2, '0'), '%m').strftime('%B')
            header = f'{month}, {year}'
            record_stat = template.format(*await self.get_stat(table, temp))
            months.append(f'<b>{header}\n</b>\n{record_stat}')
            temp = await self.get_stat_dict(table)
        return [f'{main_text}\n\n<blockquote>🗓 {month}</blockquote>' for month in months][::-1]

    async def receive_stat(self, state: FSMContext) -> dict[str, str]:
        stat_months = await self.db.get_stats()
        await state.update_data(stat=stat_months)
        return {'text': stat_months[0], **self.default_args}
