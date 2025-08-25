import sqlite3 as sq
from typing import Any

import pytz
from datetime import datetime
import locale

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from broadcast import Broadcast
from utils_funcs import find_resource_path, create_input_file


class DBUtils:
    def __init__(self, config):
        self.db = sq.connect(find_resource_path('data/bot.db'))
        self.cur = self.db.cursor()
        self.__dict__.update({key: config.jsons[key] for key in ['keyboards', 'messages', 'stats']})
        self.config = config
        self.start_db()
        self.stat, self.broadcast = (Stats(self), Broadcast(self)) if config.admin_chat_id else (None, None)

    def start_db(self, *queries: list[str | list]):
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
            f'''
                INSERT INTO users (user_id)
                VALUES ("{user_id}")
                ON CONFLICT(user_id)
                DO UPDATE SET is_active = 1;
            ''')

    async def count_users(self) -> dict[str, int]:
        result = {}
        for is_active in [1, 0]:
            key = 'in' * (not is_active) + 'active'
            result[key] = len(await self.execute_query(f'SELECT * FROM users WHERE is_active = {is_active}'))
            if self.stat:
                await self.execute_query(f"UPDATE {self.stat.get_table_name()} SET count = ? WHERE button = ?",
                                         result[key], key + '_users')

        result['all'] = sum(result.values())
        return result

    async def get_active_users(self) -> list[int]:
        results = await self.execute_query('SELECT user_id FROM users WHERE is_active = 1')
        return [result[0] for result in results]

    async def update_activity(self, user_id: int | str, activity: bool = False) -> None:
        await self.execute_query(f'UPDATE users SET is_active = {int(activity)} WHERE user_id = "{user_id}"')


class Stats:
    def __init__(self, dbutils: DBUtils):
        self.dbutils = dbutils
        self.config = dbutils.config
        self.db = dbutils.db
        self.cur = self.db.cursor()
        self.admin_chat = self.config.admin_chat_id
        self.tz = pytz.timezone('Asia/Irkutsk')
        locale.setlocale(category=locale.LC_ALL, locale="Russian")
        self.base_args = {'reply_markup': self.config.keyboards.get('stat'), **self.config.default_args}
        self.start_db()
        self.router = self.set_router()

    def start_db(self) -> None:
        table = self.get_table_name()
        self.cur.execute(f'''
                            CREATE TABLE IF NOT EXISTS {table} (
                            button text PRIMARY KEY,
                            count INTEGER DEFAULT 0
                            )
                        ''')
        self.cur.executemany(f'INSERT OR IGNORE INTO {table} (button) VALUES (?)',
                             [(btn,) for btn in self.config.jsons['stats'] + ['active_users', 'inactive_users']])
        self.db.commit()

    def set_router(self) -> Router:
        router = Router()

        @router.message(Command('stat'), F.chat.id == self.config.admin_chat_id)
        async def stat_cmd(message: Message, state: FSMContext):
            await message.delete()
            await message.answer(**await self.format_stat(state))

        @router.message(Command('db'), F.chat.id == self.config.admin_chat_id)
        async def db_cmd(message: Message):
            await message.delete()
            await message.answer_document(create_input_file(self.config.data_folder / 'bot.db'),
                                          caption='База данных <b>успешно</b> выгружена ✅', parse_mode='HTML')

        @router.callback_query(F.data == 'stat')
        async def stat(callback: CallbackQuery, state: FSMContext):
            try:
                await callback.message.edit_text(**await self.format_stat(state))
            except TelegramBadRequest:
                await callback.answer('Вы на первой странице 🏠')

        @router.callback_query(F.data.startswith('stat'))
        async def stat_scroll(callback: CallbackQuery, state: FSMContext):
            stats = (await state.get_data()).get('stat') or await self.get_stats()
            current = stats.index(callback.message.html_text)
            current += 1 if callback.data.endswith('forward') else -1
            if 0 <= current < len(stats):
                await callback.message.edit_text(stats[current], **self.base_args)
            else:
                await callback.answer('Больше значений нет 😢')

        return router

    def get_stat_name(self, stat: str) -> str | None:
        for key, value in self.config.jsons['keyboards'].items():
            for callback, text in value.items():
                if callback == stat:
                    return text
        return None

    def get_table_name(self) -> str:
        now = datetime.now(tz=self.tz)
        month, year = now.month, now.year
        return f'stats_{month}_{year}'

    async def get_table(self, table_name: str) -> dict[str, int]:
        table_name = table_name or self.get_table_name()
        entries = await self.dbutils.execute_query(f'SELECT * FROM {table_name}')
        result = {}
        for entry in entries:
            btn = entry[0]
            result[self.get_stat_name(btn) or btn] = entry[1]
        return result

    async def get_stat(self, table_name: str = '', temp: dict[str, int] = None) -> tuple[int, Any, int | Any, str]:
        if temp is None:
            temp = {}
        table = await self.get_table(table_name)
        result, total, users = [], 0, []
        for text, count in table.items():
            if text.endswith('users'):
                count -= temp.get(text) or 0
                users.append(count)
            else:
                result.append(f'— «{text}»: {count}')
                total += count
        return sum(users), *users, total, '\n'.join(result)

    async def get_stats(self) -> list[str]:
        template = self.config.messages.get('stat')['text']
        main_text = self.config.messages.get('all_stat')['text'].format(*await self.get_stat())
        months = []
        temp = {}
        tables = await self.dbutils.execute_query('SELECT name FROM sqlite_master WHERE type="table"')
        for table in tables:
            table = table[0]
            if not table.startswith('stats'):
                continue
            month_number, year = table.replace('stats_', '').split('_')
            month = datetime.strptime(month_number.rjust(2, '0'), '%m').strftime('%B')
            header = f'{month}, {year}'
            record_stat = template.format(*await self.get_stat(table, temp))
            months.append(f'<b>{header}\n</b>\n{record_stat}')
            temp = await self.get_table(table)
        return [f'{main_text}\n\n<blockquote>🗓 {month}</blockquote>' for month in months][::-1]

    async def format_stat(self, state: FSMContext) -> dict[str, str]:
        stat_months = await self.get_stats()
        await state.update_data(stat=stat_months)
        return {'text': stat_months[0], **self.base_args}

    async def increase_stat(self, button: str) -> None:
        await self.dbutils.execute_query(f'UPDATE {self.get_table_name()} SET count = count + 1 WHERE button = {button}')
