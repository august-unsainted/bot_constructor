import asyncio
from typing import Any, Callable, Union

from aiogram import Router, Bot, F
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError, AiogramError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, InlineKeyboardMarkup, CallbackQuery, User, InputMediaPhoto, InputMediaVideo


class States(StatesGroup):
    message_id = State()
    text = State()
    media = State()
    media_type = State()


class Broadcast:
    def __init__(self, db):
        """Инициализация класса рассылки, загрузка конфигов и клавиатур."""
        self.db = db
        self.config = self.db.config
        kbs = self.config.keyboards
        self.keyboards = {name.split('_')[-1]: kb for name, kb in kbs.items() if name.startswith('broadcast')}
        self.texts = self.config.jsons.get('broadcast')
        self.base_args = self.config.default_args
        self.router = self.register_handlers()

    def register_handlers(self) -> Router:
        """Регистрирует обработчики сообщений и колбэков для рассылки."""
        router = Router()

        router.message.register(self.start_broadcast, Command('mail'), F.chat.id == self.config.admin_chat)
        router.callback_query.register(self.start_broadcast, F.data == 'broadcast')

        @router.callback_query(F.data == 'broadcast_cancel')
        async def cancel(callback: CallbackQuery, state: FSMContext):
            await state.clear()
            await callback.message.delete()

        @router.message(States.text)
        async def handle_text(message: Message, state: FSMContext, bot: Bot):
            await message.delete()
            await state.update_data(text=message.text)
            await self.prompt_media(message, state, bot)

        media_kb = self.keyboards.get('media')

        @router.message(States.media)
        async def handle_media_input(message: Message, state: FSMContext, bot: Bot):
            await message.delete()
            if not (message.photo or message.video):
                return await self.prompt_media(message, state, bot)

            caption = await self.format_preview(state)
            has_photo = message.photo is not None
            media_file = (message.photo[-1] if has_photo else message.video).file_id
            media_type = 'photo' if has_photo else 'video'
            args = { "media": media_file, "caption": caption, **self.base_args}
            media = InputMediaPhoto(**args) if has_photo else InputMediaVideo(**args)

            await state.update_data(media_type=media_type, media=media_file)
            await bot.edit_message_media(media=media, **await self.get_chat_info(message, state, media_kb))
            return None

        @router.callback_query(F.data == 'skip_pictures')
        async def skip_pictures(callback: CallbackQuery, state: FSMContext):
            await state.update_data(media=None)
            preview_text = await self.format_preview(state)
            await callback.message.edit_text(preview_text, reply_markup=media_kb, **self.base_args)

        @router.callback_query(F.data == 'broadcast_confirm')
        async def confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
            data = await state.get_data()
            await state.clear()

            admin_params = {**await self.get_chat_info(callback.message), **self.base_args}
            await self.update_status_msg(callback.message.bot, self.texts.get('sending'), data, admin_params)

            params = {key: data[key] for key in ['text', 'media', 'media_type']}
            await self.execute_broadcast(bot, callback.from_user, admin_params, params)

        return router

    async def prompt_media(self, message: Message, state: FSMContext, bot: Bot) -> None:
        """Отправляет запрос на прикрепление медиафайла."""
        text = self.texts.get('text').format(message.text)
        args = await self.get_chat_info(message, state, self.keyboards['text'])
        await bot.edit_message_text(text, **args, **self.base_args)
        await state.set_state(States.media)

    async def format_preview(self, state: FSMContext) -> str:
        """Формирует текст предпросмотра рассылки с количеством получателей."""
        data = await state.get_data()
        return self.texts.get('media').format(data.get('text'), await self.count_active())

    async def send_safe(self, user_id: str, func: Callable, params: dict[str, str]) -> bool:
        """Выполняет безопасную отправку сообщения с обработкой ошибок и лимитов."""
        try:
            await func(chat_id=user_id, **params)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            return await self.send_safe(user_id, func, params)
        except (TelegramAPIError, AiogramError) as e:
            print(f"Ошибка отправки пользователю {user_id}: {e}")
            await self.db.update_activity(user_id)
        else:
            return True
        return False

    async def execute_broadcast(self, bot: Bot, sender: User, admin_params: dict, broadcast_params: dict) -> None:
        """Запускает процесс массовой рассылки сообщений пользователям."""
        count = 0
        batch_size = 500
        semaphore = asyncio.Semaphore(20)

        media = broadcast_params['media']
        media_type = broadcast_params.get('media_type')
        args = self.get_content_payload(broadcast_params)
        args['reply_markup'] = self.keyboards.get('receive')

        if media:
            func = bot.send_photo if media_type == 'photo' else bot.send_video
            args[media_type] = media
        else:
            func = bot.send_message

        async def send(user_id):
            async with semaphore:
                return await self.send_safe(user_id, func, args)

        users = await self.db.get_active_users()
        try:
            for i in range(0, len(users), batch_size):
                batch = users[i:i + batch_size]
                results = await asyncio.gather(*(send(user_id) for user_id in batch))
                count += sum(results)
            await self.db.count_users()
        finally:
            text = self.texts.get('result').format(broadcast_params['text'], count, sender.username)
            await self.update_status_msg(bot, text, broadcast_params, admin_params)

    @staticmethod
    async def update_status_msg(bot: Bot, text: str, args: dict, admin_args: dict = None) -> None:
        """Обновляет сообщение в админке (меняет текст или подпись в зависимости от типа)."""
        message_args = Broadcast.get_content_payload(args, args=admin_args.copy(), text=text)
        func = bot.edit_message_caption if args.get('media') else bot.edit_message_text
        try:
            await func(**message_args)
        except TelegramBadRequest:
            # Если сообщение нельзя отредактировать (например, устарело), отправляем новое
            admin_args = admin_args.copy()
            admin_args.pop('message_id', '')
            await bot.send_message(text=text, **admin_args)

    async def count_active(self) -> int:
        """Возвращает количество активных пользователей из базы данных."""
        return len(await self.db.get_active_users())

    @staticmethod
    async def get_chat_info(message: Message, state: FSMContext = None, kb: InlineKeyboardMarkup = None) -> dict[
        str, Any]:
        """Формирует словарь с chat_id, message_id и клавиатурой для дальнейшей отправки."""
        message_id = (await state.get_data()).get('message_id') if state else message.message_id
        args = {'chat_id': message.chat.id, 'message_id': message_id}
        if kb:
            args['reply_markup'] = kb
        return args

    @staticmethod
    def get_content_payload(data: dict, args: dict = None, text: str = None) -> dict[str, Any]:
        """Определяет ключ (text или caption) и формирует аргументы сообщения."""
        args = args or {}
        key = 'caption' if data.get('media') else 'text'
        args[key] = text or data.get('text')
        return args

    async def start_broadcast(self, event: Union[Message, CallbackQuery], state: FSMContext):
        """Начинает рассылку: очищает состояние и запрашивает текст."""
        await state.clear()
        args = {
            'text': self.texts.get('start').format(await self.count_active()),
            'reply_markup': self.keyboards.get('cancel')
        }

        if isinstance(event, Message):
            response = await event.answer(**args)
            await event.delete()
        else:
            response = await self.config.handle_edit_message(event.message, args)

        await state.update_data(message_id=response.message_id)
        await state.set_state(States.text)
