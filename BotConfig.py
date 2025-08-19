import orjson
from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InputMediaPhoto, Message, CallbackQuery
from aiogram.filters import Command
from accessify import private, protected

from DBUtils import DBUtils
from utils.filesystem import create_input_file, find_resource_path
from utils_funcs import *


class BotConfig:
    def __init__(self, data_folder: Path = None, default_answer: str = '', default_args: dict = None, back_exclusions: tuple = None, admin_chat_id: int = None) -> None:
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

        self.data_folder = data_folder or Path.cwd().parent / 'data'
        self.default_answer = default_answer
        self.default_args = default_args or {'parse_mode': 'HTML', 'disable_web_page_preview': True}
        self.back_exclusions = back_exclusions or ('start', 'broadcast', 'stat')
        self.admin_chat_id = admin_chat_id
        self.jsons = self.load_jsons()
        self.keyboards = self.generate_keyboards()
        self.images = self.load_images()
        self.messages = self.load_messages()
        self.db = DBUtils(self)
        self.router, self.stat_router = self.set_routers()

    @private
    def get_previous_section(self, needle: str) -> str | None:
        for key, value in self.jsons['keyboards'].items():
            for callback in value.keys():
                if callback == needle:
                    return key
        return None

    def generate_keyboards(self) -> dict[str, InlineKeyboardMarkup]:
        kbs = {}
        for key, kb in self.jsons['keyboards'].items():
            if key.endswith(self.back_exclusions) or 'back' in kb:
                back = None
            else:
                back = self.get_previous_section(key)
            kbs[key] = generate_kb(back, kb)
        kbs['stat'] = InlineKeyboardMarkup(inline_keyboard=[[row[0] for row in kbs.get('stat').inline_keyboard]])
        return kbs

    def load_images(self) -> dict:
        imgs = {}
        img_folder = self.data_folder / 'images'
        src_dir = Path(find_resource_path(img_folder))
        for root, _, files in src_dir.walk():
            for file in files:
                fsinput = create_input_file(img_folder / file)
                file = (root / file).stem
                imgs[file] = InputMediaPhoto(media=fsinput, caption=self.jsons['messages'].get(file), parse_mode='HTML')
                if file == 'start':
                    imgs['cmd_start'] = fsinput
        return imgs

    def load_jsons(self) -> dict[str, dict[str, str]]:
        json_dir = self.data_folder / 'json'
        result = {}
        for root, _, files in json_dir.walk():
            for file in files:
                if file.endswith('.json'):
                    file_path = json_dir / file
                    data = orjson.loads(file_path.read_bytes())
                    result[file[:-5]] = next(iter(data.values())) if len(data) == 1 else data
        return result

    def load_messages(self) -> dict[str, dict[str, str | InlineKeyboardMarkup | FSInputFile]]:
        raw_messages = self.jsons['messages']
        messages = {
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
            messages[callback] = args
        return messages

    def set_routers(self) -> tuple[Router, Router]:
        router = Router()

        if self.default_answer:
            @router.message()
            async def handle_messages(message: Message):
                await message.answer(self.default_answer)

        @router.callback_query()
        async def handle_callback(callback: CallbackQuery):
            await self.handle_message(callback)

        stat_router = Router()

        if self.admin_chat_id is not None:
            @stat_router.message(Command('stat'), F.chat.id == self.admin_chat_id)
            async def stat_cmd(message: Message, state: FSMContext):
                await message.delete()
                await message.answer(**await self.db.receive_stat(state))

            @stat_router.message(Command('db'), F.chat.id == self.admin_chat_id)
            async def db_cmd(message: Message):
                await message.delete()
                await message.answer_document(create_input_file(self.data_folder / 'bot.db'),
                                              caption='База данных <b>успешно</b> выгружена ✅', parse_mode='HTML')

            @stat_router.callback_query(F.data == 'stat')
            async def stat(callback: CallbackQuery, state: FSMContext):
                try:
                    await callback.message.edit_text(**await self.db.receive_stat(state))
                except TelegramBadRequest:
                    await callback.answer('Вы на первой странице 🏠')

            @stat_router.callback_query(F.data.startswith('stat'))
            async def stat_scroll(callback: CallbackQuery, state: FSMContext):
                stats = (await state.get_data()).get('stat') or await self.db.get_stats()
                current = stats.index(callback.message.html_text)
                current += 1 if callback.data.endswith('forward') else -1
                if 0 <= current < len(stats):
                    await callback.message.edit_text(stats[current], **self.default_args)
                else:
                    await callback.answer('Больше значений нет 😢')

        return router, stat_router

    async def handle_message(self, callback: CallbackQuery, additional: dict = None) -> None:
        args = self.messages.get(callback.data) or self.default_args
        if additional:
            args = {**args, **additional}

        if args.get('media'):
            await callback.message.edit_media(**args)
        else:
            await self.handle_edit_message(callback.message, args)

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
