import asyncio
from typing import Any, Callable

from aiogram import Router, Bot, F
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError, AiogramError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, InlineKeyboardMarkup, CallbackQuery, User, InputMediaPhoto

from db_utils import DBUtils
from utils.db import update_activity


class States(StatesGroup):
    message_id = State()
    text = State()
    media = State()

class Broadcast:
    def __init__(self, db: DBUtils):
        self.db = db
        self.config = self.db.config
        kbs = self.config.keyboards
        self.keyboards = {kb: kbs.get(f'{kb}_broadcast') for kb in ['cancel', 'edit', 'confirm']}
        self.keyboards['receive'] = kbs.get('broadcast')
        self.messages = self.config.jsons['messages']
        self.base_args = self.config.default_args

    async def get_state_args(self, state: FSMContext, message: Message, kb: InlineKeyboardMarkup) -> dict[str, Any]:
        data = await state.get_data()
        return {'chat_id': message.chat.id, 'message_id': data['message_id'], 'reply_markup': kb, **self.db.config.default_args}

    def get_args(self, text: str, kb: InlineKeyboardMarkup) -> dict[str, InlineKeyboardMarkup | str]:
        return {'text': text, 'reply_markup': kb}

    async def handle_broadcast(self, context: Message | CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        args = {'text': self.messages.get('broadcast').format(await self.db.get_active_users()),
                'reply_markup': self.keyboards['cancel']}
        if isinstance(context, Message):
            response = await context.answer(**args)
        else:
            response = await self.config.handle_text_edit(context.message, args)
        await state.update_data(message_id=response.message_id)
        await state.set_state(States.text)

    async def get_media(self, message: Message, state: FSMContext, bot: Bot) -> None:
        await bot.edit_message_text(self.messages.get('broadcast_text').format(message.text),
                                    **await self.get_state_args(state, message, self.keyboards['edit']))
        await state.set_state(States.media)

    async def get_result(self, state: FSMContext) -> str:
        data = await state.get_data()
        return self.messages.get('broadcast_result').format(data['text'], )

    async def send_message(self, user_id: str, func: Callable, params: dict[str, str]) -> bool:
        try:
            await func(chat_id=user_id, **params)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            return await self.send_message(user_id, func, params)
        except (TelegramAPIError, AiogramError) as e:
            print(f"Ошибка отправки пользователю {user_id}: {e}")
            await update_activity(user_id)
        else:
            return True
        return False

    async def send_broadcast(self, bot: Bot, sender: User, admin_params: dict, broadcast_params: dict) -> None:
        count = 0
        try:
            semaphore = asyncio.Semaphore(20)

            async def send(user_id):
                async with semaphore:
                    message_text, media = broadcast_params['text'], broadcast_params['media']
                    args = {'reply_markup': self.keyboards['receive'], **self.base_args}
                    if media:
                        func = bot.send_photo
                        args['photo'], args['caption'] = media, message_text
                    else:
                        func = bot.send_message
                        args['text'] = message_text
                    return await self.send_message(user_id, func, args)

            tasks = [asyncio.create_task(send(user_id)) for user_id in await self.db.get_active_users()]
            results = await asyncio.gather(*tasks)
            count = sum(1 for success in results if success)
            await self.db.count_users()
        finally:
            text = self.messages.get('broadcast_end').format(broadcast_params['text'], count, sender.first_name,
                                                             sender.username)
            try:
                if broadcast_params['media']:
                    await bot.edit_message_caption(caption=text, parse_mode='HTML', **admin_params)
                else:
                    await bot.edit_message_text(text=text, parse_mode='HTML', **admin_params)
            except TelegramBadRequest:
                await bot.send_message(text=text, parse_mode='HTML', **admin_params)

    def set_router(self):
        router = Router()

        @router.message(Command('mail'), F.chat.id == self.config.admin_chat_id)
        async def cmd_mail(message: Message, state: FSMContext):
            await self.handle_broadcast(message, state)

        @router.callback_query(F.data == 'broadcast')
        async def broadcast(callback: CallbackQuery, state: FSMContext):
            await self.handle_broadcast(callback, state)

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
            await bot.edit_message_media(
                media=InputMediaPhoto(media=media, parse_mode='HTML', caption=await self.get_result(state)),
                **await self.get_state_args(state, message, confirm_kb))

        @router.callback_query(F.data == 'skip_pictures')
        async def skip_pictures(callback: CallbackQuery, state: FSMContext):
            await state.update_data(media='')
            await callback.message.edit_text(await self.get_result(state), parse_mode='HTML', reply_markup=confirm_kb)

        @router.callback_query(F.data == 'confirm_broadcast')
        async def confirm_broadcast(callback: CallbackQuery, state: FSMContext, bot: Bot):
            data = await state.get_data()
            await state.clear()
            if data['media']:
                await callback.message.edit_caption(caption=f'⏳ <b>Рассылка в процессе…</b>', parse_mode='HTML')
            else:
                await callback.message.edit_text(f'⏳ <b>Рассылка в процессе…</b>', parse_mode='HTML')

            admin_params = {'message_id': callback.message.message_id, 'chat_id': callback.message.chat.id, **self.base_args}
            params = {key: data[key] for key in ['text', 'media']}

            await self.send_broadcast(bot, callback.from_user, admin_params, params)

        return router
