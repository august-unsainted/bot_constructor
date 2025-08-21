from pathlib import Path

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart

import config
from bot_config import BotConfig

router = Router()
bot_config = BotConfig(default_answer='эщкере', admin_chat_id=config.ADMIN)


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer_photo(**bot_config.messages.get('cmd_start'))


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(str(message.chat.id))
