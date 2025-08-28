from typing import Callable

import orjson
from aiogram import Router, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import InputMediaPhoto, Message, CallbackQuery
from accessify import private

from bot_constructor.db_utils import DBUtils
from bot_constructor.utils_funcs import *


class BotConfig:
    def __init__(self, data_folder: Path = None, default_answer: str = '', default_args: dict = None, back_exclusions: tuple = None, admin_chat_id: int | str = None) -> None:
        """
        Создает быструю конфигурацию бота из JSON файлов.

        :param data_folder: Путь к основной директории с данными бота. По умолчанию: '/data'
        :type data_folder: Path, optional
        :param default_answer: Ответ бота по умолчанию (на все сообщения, кроме команд)
        :type default_answer: str, optional
        :param default_args: Базовые аргументы сообщений, например, parse_mode. По умолчанию: parse_mode = HTML, disable_web_page_preview = True
        :type default_args: dict, optional
        :param back_exclusions: Callback-данные сообщений, у которых не должно быть кнопки назад (проверяется через endswith). По умолчанию: start, broadcast, stat
        :type back_exclusions: tuple, optional
        """

        self.data_folder = data_folder or Path.cwd() / 'data'
        self.default_answer = default_answer
        self.default_args = default_args or {'parse_mode': 'HTML'}
        self.back_exclusions = back_exclusions or ('start', 'broadcast', 'stat')
        self.admin_chat_id = int(admin_chat_id) if admin_chat_id else None
        self.jsons = self.keyboards = self.images = self.messages = None
        self.load_all()
        self.db = DBUtils(self)
        self.router = self.set_router()
        self.stat_router = self.db.stat.router if self.db.stat else None
        self.broadcast_router = self.db.broadcast.router if self.db.broadcast else None

    @staticmethod
    def find_needle(key: str, kb: dict, needle: str) -> str | None:
        for callback, text in kb.items():
            if callback == needle:
                return key
            elif isinstance(text, dict):
                result = BotConfig.find_needle(key, text, needle)
                if result:
                    return result
        return None

    @private
    def get_previous_section(self, needle: str) -> str | None:
        for key, value in self.jsons['keyboards'].items():
            result = self.find_needle(key, value, needle)
            if result:
                return result
        return None

    @private
    def load_all(self):
        self.load_jsons()
        self.load_keyboards()
        self.load_images()
        self.load_messages()

    @staticmethod
    def load_files(target_dir: Path, func: Callable) -> dict:
        result = {}
        for root, _, files in target_dir.walk():
            for file in files:
                file_path = root / file
                func(result, file_path)
        return result

    def load_images(self) -> None:
        img_folder = self.data_folder / 'images'
        src_dir = Path(find_resource_path(img_folder))

        def append_file(result: dict, file_path: Path):
            fsinput = create_input_file(file_path)
            file = file_path.stem
            caption = self.jsons['messages'].get(file)
            result[file] = InputMediaPhoto(media=fsinput, caption=caption, parse_mode='HTML')
            if file == 'start':
                result['cmd_start'] = fsinput

        self.images = self.load_files(src_dir, append_file)

    def load_jsons(self) -> None:
        json_dir = self.data_folder / 'json'

        def append_file(result: dict, file_path: Path):
            data = orjson.loads(file_path.read_bytes())
            result[file_path.stem] = next(iter(data.values())) if len(data) == 1 else data
        self.jsons = self.load_files(json_dir, append_file)

    def load_keyboards(self) -> None:
        self.keyboards = {}
        for key, kb in self.jsons['keyboards'].items():
            if key.endswith(self.back_exclusions) or 'back' in kb:
                back = None
            else:
                back = self.get_previous_section(key)
            self.keyboards[key] = generate_kb(back, kb)
        if self.keyboards.get('stat'):
            self.keyboards['stat'] = InlineKeyboardMarkup(inline_keyboard=[[row[0] for row in self.keyboards.get('stat').inline_keyboard]])

    def load_messages(self) -> None:
        raw_messages = self.jsons['messages']
        self.messages = {
            'cmd_start': {
                'photo':        self.images.get('cmd_start'), 'caption': raw_messages.get('start'),
                'reply_markup': self.keyboards.get('start'), **self.default_args
            }
        }
        for callback in raw_messages.keys():
            args = {**self.default_args,
                    'reply_markup': self.keyboards.get(callback) or generate_kb(self.get_previous_section(callback))}
            if self.images.get(callback):
                args['media'] = self.images.get(callback)
            else:
                args['text'] = raw_messages.get(callback)
            self.messages[callback] = args

    def set_router(self) -> Router:
        router = Router()

        @router.message(CommandStart())
        async def cmd_start(message: Message):
            # await message.answer(str(message.chat.id))
            start_message = self.messages.get('cmd_start')
            if 'photo' in start_message:
                await message.answer_photo(**start_message)
            else:
                await message.answer(**start_message)
            await self.db.add_user(message.from_user.id)

        if self.default_answer:
            @router.message()
            async def handle_messages(message: Message):
                await message.answer(self.default_answer)

        @router.callback_query()
        async def handle_callback(callback: CallbackQuery):
            await self.handle_message(callback)

        return router

    async def handle_message(self, callback: CallbackQuery, additional: dict = None) -> None:
        args = self.messages.get(callback.data) or self.default_args
        if additional:
            args = {**args, **additional}

        if args.get('media'):
            await callback.message.edit_media(**args)
        else:
            await self.handle_edit_message(callback.message, args)

    def include_routers(self, dp: Dispatcher):
        routers = [router for router in [self.stat_router, self.broadcast_router] if router]
        dp.include_routers(*routers, self.router)

    @staticmethod
    async def handle_edit_message(message: Message, args: dict):
        if message.text:
            response = await message.edit_text(**args)
        else:
            response = await message.answer(**args)
            try:
                await message.delete()
            except TelegramBadRequest:
                pass
        return response
