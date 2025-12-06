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

from bot_constructor.broadcast import Broadcast
from bot_constructor.utils_funcs import find_resource_path, create_input_file


class DBUtils:
    def __init__(self, config, stats_exclusions: list[str]):
        self.db = sq.connect(find_resource_path('data/bot.db'))
        self.db.row_factory = sq.Row
        self.cur = self.db.cursor()
        self.__dict__.update(
            {key: config.jsons[key] for key in ['keyboards', 'messages', 'stats'] if key in config.jsons})
        self.config = config
        self.start_db()
        self.stat, self.broadcast = None, None
        if config.admin_chat:
            self.stat = Stats(self, stats_exclusions)
            self.broadcast = Broadcast(self)

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

    def execute_query(self, query: str, *args: Any) -> None | list[tuple]:
        query_result = self.cur.execute(query, tuple(args))
        query = query.strip().lower()
        if query.startswith('select') or 'returning' in query:
            rows = query_result.fetchall()
            result = rows
        elif query.startswith('insert'):
            result = self.cur.lastrowid
        else:
            result = None
        self.db.commit()
        return result

    def add_user(self, user_id: int | str) -> None:
        self.execute_query(
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
            result[key] = len(self.execute_query(f'SELECT * FROM users WHERE is_active = {is_active}'))
            if self.stat:
                self.execute_query(f"UPDATE {self.stat.get_table_name()} SET count = ? WHERE button = ?",
                                   result[key], key + '_users')

        result['all'] = sum(result.values())
        return result

    async def get_active_users(self) -> list[int]:
        results = self.execute_query('SELECT user_id FROM users WHERE is_active = 1')
        return [result[0] for result in results]

    async def update_activity(self, user_id: int | str, activity: bool = False) -> None:
        self.execute_query(f'UPDATE users SET is_active = {int(activity)} WHERE user_id = "{user_id}"')


class Stats:
    def __init__(self, dbutils: DBUtils, stats_exclusions: list[str]):
        self.dbutils = dbutils
        self.config = dbutils.config
        self.db = dbutils.db
        self.cur = self.db.cursor()
        self.admin_chat = self.config.admin_chat
        self.exclusions = stats_exclusions
        self.tracks = self.config.jsons.get('stats')
        messages = [self.config.messages.get(key) for key in ['all_stat', 'stat']]
        self.general_template, self.month_template = [mess.get('text') if mess else '' for mess in messages]
        self.tz = pytz.timezone('Asia/Irkutsk')
        locale.setlocale(locale.LC_ALL, 'ru_RU.UTF-8')
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
                             [(btn,) for btn in self.tracks + ['active_users', 'inactive_users']])
        self.db.commit()

    def set_router(self) -> Router:
        router = Router()

        @router.message(Command('stat'), F.chat.id == self.admin_chat)
        async def stat_cmd(message: Message, state: FSMContext):
            await message.delete()
            await message.answer(**await self.format_stat(state))

        chat_ids = [chat for chat in [self.admin_chat, self.config.dev_chat] if chat is not None]
        @router.message(Command('db'), F.chat.id.in_(chat_ids))
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

    @staticmethod
    def find_needle(key: str, kb: dict | str, needle: str) -> str | None:
        for callback, text in kb.items():
            if callback == needle:
                return text
            elif isinstance(text, dict):
                result = Stats.find_needle(key, text, needle)
                if result:
                    return result
        return None

    def get_stat_name(self, stat: str) -> str | None:
        for key, value in self.config.jsons['keyboards'].items():
            result = self.find_needle(key, value, stat)
            if result:
                return result
        return None

    def get_table_name(self) -> str:
        if not self.month_template:
            return 'stats'
        now = datetime.now(tz=self.tz)
        month, year = now.month, now.year
        return f'stats_{month}_{year}'

    def get_table(self, table_name: str) -> dict[str, int]:
        table_name = table_name or self.get_table_name()
        entries = self.dbutils.execute_query(f'SELECT * FROM {table_name}')
        result = {}
        for entry in entries:
            btn = entry[0]
            result[btn] = entry[1]
        return result

    def get_stat(self, table_name: str = '', temp: dict[str, int] = None) -> tuple[int, Any, int | Any, str]:
        if temp is None:
            temp = {}
        table = self.get_table(table_name)
        result, total, users = [], 0, []
        for text, count in table.items():
            if text.endswith('users'):
                count -= temp.get(text) or 0
                users.append(count)
            elif text not in self.exclusions:
                text = self.get_stat_name(text) or text
                result.append(f'— «{text}»: {count}')
                total += count
        return sum(users), *users, total, '\n'.join(result)

    def get_general_stats(self):
        return self.general_template.format(*self.get_stat())

    def get_stats(self) -> list[str]:
        main_text = self.get_general_stats()
        months = []
        temp = {}
        tables = self.dbutils.execute_query('SELECT name FROM sqlite_master WHERE type="table"')
        for table in tables:
            table = table[0]
            if not table.startswith('stats'):
                continue
            month_number, year = table.replace('stats_', '').split('_')
            month = datetime.strptime(month_number, '%m').strftime('%B')
            header = f'{month}, {year}'
            record_stat = self.month_template.format(*self.get_stat(table, temp))
            months.append(f'<b>{header}\n</b>\n{record_stat}')
            temp = self.get_table(table)
        return [f'{main_text}\n\n<blockquote>🗓 {month}</blockquote>' for month in months][::-1]

    async def format_stat(self, state: FSMContext = None) -> dict[str, str]:
        if self.month_template:
            stat_months = self.get_stats()
            await state.update_data(stat=stat_months)
        else:
            stat_months = [self.get_general_stats()]
        return {'text': stat_months[0], **self.base_args}

    def increase_stat(self, button: str) -> None:
        self.dbutils.execute_query(f'UPDATE {self.get_table_name()} SET count = count + 1 WHERE button = "{button}"')
