from typing import Callable, Any
from copy import deepcopy

import orjson
from aiogram import Router, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import InputMediaPhoto, Message, CallbackQuery
from accessify import private

from bot_constructor.db_utils import DBUtils
from bot_constructor.utils_funcs import *


class BotConfig:
    def __init__(self, data_folder: Path = None, default_answer: str = '', default_message: str = '',
                 default_args: dict = None,
                 back_exclusions: tuple = None, admin_chat: int | str = None, dev_chat: int | str = None,
                 name_in_start: bool = False, stats_exclusions: list = None) -> None:
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
        self.default_message = default_message
        self.default_args = default_args or {'parse_mode': 'HTML'}
        self.name_in_start = name_in_start
        self.back_exclusions = (*back_exclusions, 'start', 'broadcast', 'stat')
        self.admin_chat = int(admin_chat) if admin_chat else None
        self.dev_chat = int(dev_chat) if dev_chat else None
        self.jsons = self.keyboards = self.images = self.messages = None
        self.load_all()
        self.texts = self.jsons.get('messages')
        self.db = DBUtils(self, stats_exclusions or [])
        self.router = self.set_router()
        self.stat_router = self.db.stat.router if self.db.stat else None
        self.broadcast_router = self.db.broadcast.router if self.db.broadcast else None

    @staticmethod
    def find_needle(key: str, kb: dict | str, needle: str) -> str | None:
        for callback, text in kb.items():
            if callback == needle and key != needle:
                return key
            elif isinstance(text, dict):
                result = BotConfig.find_needle(key, text, needle)
                if result:
                    return result
        return None

    @private
    def get_previous_section(self, needle: str) -> str | None:
        for key, value in self.jsons.get('keyboards').items():
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

    @staticmethod
    def generate_kb(back_callback: str = None, data: dict[str, str] = None) -> InlineKeyboardMarkup:
        kb = []
        if data:
            for callback, text in data.items():
                row_buttons = text if isinstance(text, dict) else None
                kb.append(BotConfig.generate_row(callback, text, row_buttons))
        if back_callback:
            kb.append(BotConfig.generate_row(back_callback))
        return InlineKeyboardMarkup(inline_keyboard=kb)

    def load_keyboards(self) -> None:
        self.keyboards = {}
        for key, kb in self.jsons['keyboards'].items():
            has_back = not kb.pop('back', True)
            if key.endswith(self.back_exclusions) or has_back or 'start' in kb or 'Назад' in key:
                back = None
            else:
                back = self.get_previous_section(key)
            self.keyboards[key] = self.generate_kb(back, kb)
        if self.keyboards.get('stat'):
            self.keyboards['stat'] = InlineKeyboardMarkup(
                inline_keyboard=[[row[0] for row in self.keyboards.get('stat').inline_keyboard]])

    def load_messages(self) -> None:
        raw_messages = self.jsons['messages']
        start = {'reply_markup': self.keyboards.get('start'), **self.default_args}

        if self.images.get('cmd_start'):
            start = {'photo': self.images.get('cmd_start'), 'caption': raw_messages.get('start'), **start}
        else:
            start = {'text': raw_messages.get('start'), **start}
        self.messages = {'cmd_start': start}
        for callback in raw_messages.keys():
            args = {
                **self.default_args,
                'reply_markup': self.keyboards.get(callback) or
                                self.generate_kb(self.get_previous_section(callback))
            }
            if self.images.get(callback):
                args['media'] = self.images.get(callback)
            else:
                args['text'] = raw_messages.get(callback)
            self.messages[callback] = args

    async def format_start_message(self, key: str, name: str):
        message = self.messages.get(key)
        has_photo = 'photo' in message
        if self.name_in_start:
            if 'media' in message:
                message['media'].caption = message['media'].caption.format(name)
            else:
                key = 'caption' if has_photo else 'text'
                message[key] = message[key].format(name)
        return has_photo, message

    def set_router(self) -> Router:
        router = Router()

        @router.message(CommandStart())
        async def cmd_start(message: Message):
            # await message.answer(str(message.chat.id))
            has_time, start_message = await self.format_start_message('cmd_start', message.from_user.first_name)
            if has_time:
                await message.answer_photo(**start_message)
            else:
                await message.answer(**start_message)
            await self.db.add_user(message.from_user.id)

        if self.default_answer or self.default_message:
            admin_chat = self.admin_chat or -1
            default_mess = self.default_message
            default_ans = self.default_answer

            @router.message(F.chat.id != admin_chat)
            async def handle_messages(message: Message):
                await message.answer(**self.messages.get(default_mess) if default_mess else default_ans)

        if self.name_in_start:
            @router.callback_query(F.data == 'start')
            async def handle_start(callback: CallbackQuery):
                _, message = await self.format_start_message('start', callback.from_user.first_name)
                await self.handle_message(callback, message)

        stat = self.db.stat

        @router.callback_query()
        async def handle_callback(callback: CallbackQuery):
            if stat and callback.data in stat.tracks and callback.data not in stat.exclusions:
                stat.increase_stat(callback.data)
            await self.handle_message(callback)

        return router

    async def handle_message(self, callback: CallbackQuery, additional: dict = None) -> Any:
        args = self.messages.get(callback.data) or self.default_args
        if additional:
            args = {**args, **additional}

        if args.get('media'):
            return await callback.message.edit_media(**args)
        try:
            return await self.handle_edit_message(callback.message, args)
        except TypeError:
            print(f"Нет текста сообщения для ключа {callback.data}")

    def include_routers(self, dp: Dispatcher):
        routers = [router for router in [self.stat_router, self.broadcast_router, self.router] if router]
        dp.include_routers(*routers)

    @staticmethod
    async def handle_edit_message(message: Message, args: dict):
        if message.text:
            return await message.edit_text(**args)
        response = await message.answer(**args)
        try:
            await message.delete()
            return response
        except TelegramBadRequest:
            return None

    def edit_keyboard(self, key: str, template_kb: str, key_first: bool = False):
        kb = deepcopy(self.keyboards.get(template_kb).inline_keyboard)
        for i in range(len(kb)):
            for j in range(len(kb[i])):
                btn_data = kb[i][j].callback_data
                kb[i][j].callback_data = f'{key}_{btn_data}' if key_first else f'{btn_data}_{key}'
        return InlineKeyboardMarkup(inline_keyboard=kb)

    @staticmethod
    def get_btn(callback: str, text: str = 'Назад ⬅️') -> InlineKeyboardButton:
        key = 'url' if validators.url(callback) else 'callback_data'
        return InlineKeyboardButton(text=text, **{key: callback})

    @staticmethod
    def generate_row(callback: str, text: str = 'Назад ⬅️', btns: dict = None) -> list[InlineKeyboardButton]:
        btns = btns or {callback: text}
        return [BotConfig.get_btn(callback, text) for callback, text in btns.items()]

