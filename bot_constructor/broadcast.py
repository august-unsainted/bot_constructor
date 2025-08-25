import asyncio
from typing import Any, Callable, Union

from aiogram import Router, Bot, F
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError, AiogramError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, InlineKeyboardMarkup, CallbackQuery, User, InputMediaPhoto

class States(StatesGroup):
    message_id = State()
    text = State()
    media = State()


class Broadcast:
    def __init__(self, db):
        self.db = db
        self.config = self.db.config
        kbs = self.config.keyboards
        self.keyboards = {kb: kbs.get(f'{kb}_broadcast') for kb in ['cancel', 'edit', 'confirm']}
        self.keyboards['receive'] = kbs.get('broadcast')
        self.messages = self.config.jsons['messages']
        self.base_args = self.config.default_args
        self.router = self.set_router()

    @staticmethod
    async def get_args(message: Message, state: FSMContext = None, kb: InlineKeyboardMarkup = None) -> dict[str, Any]:
        message_id = (await state.get_data()).get('message_id') if state else message.message_id
        args = {'chat_id': message.chat.id, 'message_id': message_id}
        if kb:
            args['reply_markup'] = kb
        return args

    async def get_media(self, message: Message, state: FSMContext, bot: Bot) -> None:
        await bot.edit_message_text(self.messages.get('broadcast_text').format(message.text),
                                    **await self.get_args(message, state, self.keyboards['edit']), **self.base_args)
        await state.set_state(States.media)

    async def get_result(self, state: FSMContext) -> str:
        data = await state.get_data()
        return self.messages.get('broadcast_result').format(data.get('text'), await self.get_active())

    @staticmethod
    def get_media_args(data: dict, args: dict = None, text: str = None) -> dict[str, Any]:
        args = args or {}
        key = 'caption' if data.get('media') else 'text'
        args[key] = text or data.get('text')
        return args

    async def send_message(self, user_id: str, func: Callable, params: dict[str, str]) -> bool:
        try:
            await func(chat_id=user_id, **params)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            return await self.send_message(user_id, func, params)
        except (TelegramAPIError, AiogramError) as e:
            print(f"Ошибка отправки пользователю {user_id}: {e}")
            await self.db.update_activity(user_id)
        else:
            return True
        return False

    async def send_broadcast(self, bot: Bot, sender: User, admin_params: dict, broadcast_params: dict) -> None:
        count = 0
        try:
            semaphore = asyncio.Semaphore(20)
            message_text, media = broadcast_params['text'], broadcast_params['media']
            args = self.get_media_args(broadcast_params)
            args['reply_markup'] = self.keyboards['receive']
            if media:
                func = bot.send_photo
                args['photo'] = media
            else:
                func = bot.send_message

            async def send(user_id):
                async with semaphore:
                    return await self.send_message(user_id, func, args)

            users = await self.db.get_active_users()
            batch_size = 500
            count = 0
            for i in range(0, len(users), batch_size):
                batch = users[i:i + batch_size]
                results = await asyncio.gather(*(send(user_id) for user_id in batch))
                count += sum(results)
            await self.db.count_users()
        finally:
            text = self.messages.get('broadcast_end').format(broadcast_params['text'], count, sender.first_name,
                                                             sender.username)
            await self.handle_message_edit(bot, text, broadcast_params, admin_params)

    @staticmethod
    async def handle_message_edit(bot: Bot, text: str, args: dict, admin_args: dict = None) -> None:
        try:
            message_args = Broadcast.get_media_args(args, args=admin_args.copy(), text=text)
            func = bot.edit_message_caption if args.get('media') else bot.edit_message_text
            await func(**message_args)
        except TelegramBadRequest:
            admin_args = admin_args.copy()
            if admin_args.get('message_id'):
                admin_args.pop('message_id')
            await bot.send_message(text=text, **admin_args)

    async def get_active(self) -> int:
        return len(await self.db.get_active_users())

    def set_router(self):
        router = Router()

        async def initiate_broadcast(event: Union[Message, CallbackQuery], state: FSMContext):
            await state.clear()
            args = {'text': self.messages.get('broadcast').format(await self.get_active()),
                    'reply_markup': self.keyboards['cancel']}
            if isinstance(event, Message):
                response = await event.answer(**args)
            else:
                response = await self.config.handle_edit_message(event.message, args)
            await state.update_data(message_id=response.message_id)
            await state.set_state(States.text)

        router.message.register(initiate_broadcast, Command('mail'), F.chat.id == self.config.admin_chat_id)
        router.callback_query.register(initiate_broadcast, F.data == 'broadcast')

        @router.callback_query(F.data == 'cancel_broadcast')
        async def cancel_broadcast(callback: CallbackQuery, state: FSMContext):
            await state.clear()
            await callback.message.delete()

        @router.message(States.text)
        async def get_broadcast_text(message: Message, state: FSMContext, bot: Bot):
            await message.delete()
            await state.update_data(text=message.text)
            await self.get_media(message, state, bot)

        confirm_kb = self.keyboards['confirm']

        @router.message(States.media)
        async def get_broadcast_media(message: Message, state: FSMContext, bot: Bot):
            await message.delete()
            if not message.photo:
                return await self.get_media(message, state, bot)
            media = message.photo[0].file_id
            await state.update_data(media=media)
            input_media = InputMediaPhoto(media=media, caption=await self.get_result(state), **self.base_args)
            await bot.edit_message_media(media=input_media, **await self.get_args(message, state, confirm_kb))

        @router.callback_query(F.data == 'skip_pictures')
        async def skip_pictures(callback: CallbackQuery, state: FSMContext):
            await state.update_data(media=None)
            await callback.message.edit_text(await self.get_result(state), reply_markup=confirm_kb, **self.base_args)

        @router.callback_query(F.data == 'confirm_broadcast')
        async def confirm_broadcast(callback: CallbackQuery, state: FSMContext, bot: Bot):
            data = await state.get_data()
            await state.clear()
            admin_params = {**await self.get_args(callback.message), **self.base_args}
            await self.handle_message_edit(callback.message.bot, f'⏳ <b>Рассылка в процессе…</b>', data, admin_params)
            params = {key: data[key] for key in ['text', 'media']}
            await self.send_broadcast(bot, callback.from_user, admin_params, params)

        return router
